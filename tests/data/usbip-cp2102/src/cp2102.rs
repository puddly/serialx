//! Two emulated CP2102 chips wired back to back through a null-modem cable.
//! Only the requests the Linux cp210x driver issues are implemented; anything
//! else is stalled and logged.

use std::collections::VecDeque;

use log::{debug, warn};
use tokio::sync::mpsc::UnboundedSender;

use crate::uart::{Format, Line, Parity, Receiver, RxByte, StopBits};
use crate::usbip;

pub const VID: u16 = 0x10c4;
pub const PID: u16 = 0xea60;
pub const BCD_DEVICE: u16 = 0x0100;
const MANUFACTURER: &str = "Silicon Labs";
const PRODUCT: &str = "CP2102 USB to UART Bridge Controller";
const EP_IN: u8 = 0x81;
const EP_OUT: u8 = 0x01;
const MAX_PACKET: usize = 64;
const TX_BUF: usize = 640;
const RX_BUF: usize = 576;
const RTS_FLOW_THRESHOLD: usize = 512;
const PARTNUM_CP2102: u8 = 0x02;
const ESCAPE: u8 = 0xec;

const CTL_HS_DTR_MASK: u32 = 0x3;
const CTL_HS_CTS_HANDSHAKE: u32 = 1 << 3;
const FLOW_REPL_RTS_MASK: u32 = 0x3 << 6;
const FLOW_REPL_RTS_ACTIVE: u32 = 1 << 6;
const FLOW_REPL_RTS_FLOW_CTL: u32 = 2 << 6;

const ERR_BREAK: u32 = 1 << 0;
const ERR_FRAMING: u32 = 1 << 1;
const ERR_QUEUE_OVERRUN: u32 = 1 << 3;
const ERR_PARITY: u32 = 1 << 4;
const HOLD_CTS: u32 = 1 << 0;
const HOLD_BREAK: u32 = 1 << 5;

const LSR_PARITY: u8 = 1 << 2;
const LSR_FRAME: u8 = 1 << 3;
const LSR_BREAK: u8 = 1 << 4;

#[derive(Debug, Clone, Copy)]
pub struct Setup {
    pub request_type: u8,
    pub request: u8,
    pub value: u16,
    pub index: u16,
    pub length: u16,
}

impl Setup {
    pub fn parse(b: &[u8]) -> Self {
        Setup {
            request_type: b[0],
            request: b[1],
            value: u16::from_le_bytes([b[2], b[3]]),
            index: u16::from_le_bytes([b[4], b[5]]),
            length: u16::from_le_bytes([b[6], b[7]]),
        }
    }
}

#[derive(Debug)]
pub struct Urb {
    pub seqnum: u32,
    pub direction: u32,
    pub ep: u32,
    pub length: u32,
    pub setup: [u8; 8],
    pub data: Vec<u8>,
}

#[derive(Debug)]
struct PendingIn {
    seqnum: u32,
    length: usize,
}

#[derive(Debug)]
struct PendingOut {
    seqnum: u32,
    data: Vec<u8>,
    done: usize,
}

#[derive(Debug, Default)]
pub struct Chip {
    pub serial: String,
    pub busid: String,
    pub devnum: u32,
    pub fmt: Format,
    pub enabled: bool,
    configured: u8,
    tx: VecDeque<u8>,
    inflight: VecDeque<u64>,
    rx: VecDeque<u8>,
    errors: u32,
    ctl_hs: u32,
    flow_repl: u32,
    flow_limits: [u8; 8],
    pub break_on: bool,
    embed_events: bool,
    receiver: Receiver,
    pending_in: VecDeque<PendingIn>,
    pending_out: VecDeque<PendingOut>,
    pub reply: Option<UnboundedSender<Vec<u8>>>,
}

fn string_descriptor(s: &str) -> Vec<u8> {
    let mut d = vec![0, 0x03];
    for unit in s.encode_utf16() {
        d.extend_from_slice(&unit.to_le_bytes());
    }
    d[0] = d.len() as u8;
    d
}

pub fn device_descriptor() -> Vec<u8> {
    let mut d = vec![0x12, 0x01, 0x10, 0x01, 0x00, 0x00, 0x00, MAX_PACKET as u8];
    d.extend_from_slice(&VID.to_le_bytes());
    d.extend_from_slice(&PID.to_le_bytes());
    d.extend_from_slice(&BCD_DEVICE.to_le_bytes());
    d.extend_from_slice(&[0x01, 0x02, 0x03, 0x01]);
    d
}

fn config_descriptor() -> Vec<u8> {
    vec![
        0x09, 0x02, 0x20, 0x00, 0x01, 0x01, 0x00, 0x80, 0x32, // config, 100 mA
        0x09, 0x04, 0x00, 0x00, 0x02, 0xff, 0x00, 0x00, 0x02, // interface, vendor class
        0x07, 0x05, EP_IN, 0x02, 0x40, 0x00, 0x00, // bulk in, 64 bytes
        0x07, 0x05, EP_OUT, 0x02, 0x40, 0x00, 0x00, // bulk out, 64 bytes
    ]
}

/// The rate the 48 MHz baud generator really produces for a request.
fn actual_baud(requested: u32) -> u32 {
    let prescale = if requested <= 365 { 4.0 } else { 1.0 };
    let div = (48_000_000.0 / (2.0 * prescale * requested as f64))
        .round()
        .max(1.0);
    (48_000_000.0 / (2.0 * prescale * div)).round() as u32
}

impl Chip {
    fn dtr_out(&self) -> bool {
        self.ctl_hs & CTL_HS_DTR_MASK != 0
    }

    fn rts_out(&self) -> bool {
        match self.flow_repl & FLOW_REPL_RTS_MASK {
            FLOW_REPL_RTS_ACTIVE => true,
            FLOW_REPL_RTS_FLOW_CTL => self.rx.len() < RTS_FLOW_THRESHOLD,
            _ => false,
        }
    }

    fn cts_handshake(&self) -> bool {
        self.ctl_hs & CTL_HS_CTS_HANDSHAKE != 0
    }

    fn out_queue(&self) -> usize {
        self.tx.len() + self.inflight.len()
    }

    fn set_line_ctl(&mut self, v: u16) {
        self.fmt.data_bits = ((v >> 8) & 0xf) as u8;
        self.fmt.parity = match (v >> 4) & 0xf {
            0 => Parity::None,
            1 => Parity::Odd,
            2 => Parity::Even,
            3 => Parity::Mark,
            _ => Parity::Space,
        };
        self.fmt.stop_bits = match v & 0xf {
            0 => StopBits::One,
            1 => StopBits::OneAndHalf,
            _ => StopBits::Two,
        };
    }

    fn push_rx(&mut self, b: RxByte) {
        let lsr = (b.brk as u8 * LSR_BREAK)
            | (b.framing as u8 * LSR_FRAME)
            | (b.parity_err as u8 * LSR_PARITY);
        let encoded: Vec<u8> = match (self.embed_events, lsr, b.data) {
            (false, _, data) => vec![data],
            (true, 0, ESCAPE) => vec![ESCAPE, 0x00],
            (true, 0, data) => vec![data],
            (true, lsr, data) => vec![ESCAPE, 0x01, lsr, data],
        };
        if b.brk {
            self.errors |= ERR_BREAK;
        } else if b.framing {
            self.errors |= ERR_FRAMING;
        }
        if b.parity_err {
            self.errors |= ERR_PARITY;
        }
        if self.rx.len() + encoded.len() > RX_BUF {
            if self.errors & ERR_QUEUE_OVERRUN == 0 {
                warn!("[{}] receive buffer overrun", self.serial);
            }
            self.errors |= ERR_QUEUE_OVERRUN;
            return;
        }
        self.rx.extend(encoded);
    }

    /// Moves bulk OUT packets into the transmit buffer while it has room.
    fn fill_tx(&mut self) {
        while let Some(pending) = self.pending_out.front_mut() {
            let room = TX_BUF - self.tx.len() - self.inflight.len();
            let packet = (pending.data.len() - pending.done).min(MAX_PACKET);
            if room < packet {
                break;
            }
            self.tx
                .extend(&pending.data[pending.done..pending.done + packet]);
            pending.done += packet;
            if pending.done == pending.data.len() {
                let seqnum = pending.seqnum;
                let len = pending.data.len();
                self.pending_out.pop_front();
                debug!("[{}] bulk out #{seqnum} done: {len} bytes", self.serial);
                self.send(usbip::ret_submit(seqnum, 0, len, &[]));
            }
        }
    }

    fn send(&self, packet: Vec<u8>) {
        if let Some(reply) = &self.reply {
            let _ = reply.send(packet);
        }
    }

    /// Standard (chapter 9) requests. `Err(())` means STALL.
    fn standard(&mut self, setup: &Setup) -> Result<Vec<u8>, ()> {
        let reply = match setup.request {
            0x00 => vec![0, 0],
            0x01 | 0x03 | 0x05 | 0x0b => vec![],
            0x06 => match ((setup.value >> 8) as u8, setup.value as u8) {
                (1, _) => device_descriptor(),
                (2, _) => config_descriptor(),
                (3, 0) => vec![0x04, 0x03, 0x09, 0x04],
                (3, 1) => string_descriptor(MANUFACTURER),
                (3, 2) => string_descriptor(PRODUCT),
                (3, 3) => string_descriptor(&self.serial),
                _ => return Err(()),
            },
            0x08 => vec![self.configured],
            0x09 => {
                self.configured = setup.value as u8;
                vec![]
            }
            0x0a => vec![0],
            _ => return Err(()),
        };
        Ok(reply)
    }
}

/// One null-modem pair. `pair` keeps serials, bus IDs and device numbers
/// unique when several simulations run side by side.
pub struct Sim {
    pub chips: [Chip; 2],
    lines: [Line; 2],
}

impl Sim {
    pub fn new(pair: usize) -> Self {
        let chip = |i: usize| Chip {
            serial: format!("{}{pair}", ["left", "right"][i]),
            busid: format!("1-{}", 2 * pair + i + 1),
            devnum: (2 * pair + i) as u32 + 2,
            ..Chip::default()
        };
        Sim {
            chips: [chip(0), chip(1)],
            lines: [Line::default(), Line::default()],
        }
    }

    pub fn find(&self, busid: &str) -> Option<usize> {
        self.chips.iter().position(|c| c.busid == busid)
    }

    pub fn devid(&self, dev: usize) -> u32 {
        (1 << 16) | self.chips[dev].devnum
    }

    /// CTS, DSR, DCD as seen by `dev`: the peer's RTS and DTR.
    fn peer_pins(&self, dev: usize) -> (bool, bool, bool) {
        let peer = &self.chips[1 - dev];
        (peer.rts_out(), peer.dtr_out(), peer.dtr_out())
    }

    pub fn detach(&mut self, dev: usize) {
        let chip = &mut self.chips[dev];
        chip.pending_in.clear();
        chip.pending_out.clear();
        chip.reply = None;
        chip.configured = 0;
    }

    pub fn unlink(&mut self, dev: usize, seqnum: u32) -> bool {
        let chip = &mut self.chips[dev];
        let before = chip.pending_in.len() + chip.pending_out.len();
        chip.pending_in.retain(|p| p.seqnum != seqnum);
        chip.pending_out.retain(|p| p.seqnum != seqnum);
        before != chip.pending_in.len() + chip.pending_out.len()
    }

    pub fn submit(&mut self, dev: usize, urb: Urb) {
        let real_ep = if urb.direction == 0 {
            urb.ep as u8
        } else {
            urb.ep as u8 | 0x80
        };
        match real_ep {
            0x00 | 0x80 => {
                let setup = Setup::parse(&urb.setup);
                debug!("[{dev}] control {setup:02x?} data={:02x?}", urb.data);
                let kind = (setup.request_type >> 5) & 3;
                let reply = match kind {
                    0 => self.chips[dev].standard(&setup),
                    2 => self.vendor(dev, &setup, &urb.data),
                    _ => Err(()),
                };
                let packet = match reply {
                    Ok(mut reply) if setup.request_type & 0x80 != 0 => {
                        reply.truncate(setup.length as usize);
                        usbip::ret_submit(urb.seqnum, 0, reply.len(), &reply)
                    }
                    Ok(_) => usbip::ret_submit(urb.seqnum, 0, urb.data.len(), &[]),
                    Err(()) => {
                        warn!("stall: {setup:02x?}");
                        usbip::ret_submit(urb.seqnum, usbip::EPIPE, 0, &[])
                    }
                };
                self.chips[dev].send(packet);
            }
            EP_OUT => {
                debug!("[{dev}] bulk out #{} {} bytes", urb.seqnum, urb.data.len());
                // The chip ACKs OUT packets as soon as its buffer has room, so
                // the data is on the wire without waiting for the next frame
                self.chips[dev].pending_out.push_back(PendingOut {
                    seqnum: urb.seqnum,
                    data: urb.data,
                    done: 0,
                });
                self.chips[dev].fill_tx();
            }
            EP_IN => {
                debug!("[{dev}] bulk in #{} up to {} bytes", urb.seqnum, urb.length);
                self.chips[dev].pending_in.push_back(PendingIn {
                    seqnum: urb.seqnum,
                    length: urb.length as usize,
                })
            }
            _ => {
                warn!("urb to unknown endpoint {real_ep:#04x}");
                self.chips[dev].send(usbip::ret_submit(urb.seqnum, usbip::EPIPE, 0, &[]));
            }
        }
    }

    fn vendor(&mut self, dev: usize, setup: &Setup, data: &[u8]) -> Result<Vec<u8>, ()> {
        let recipient = setup.request_type & 0x1f;
        if recipient == 0 {
            return match (setup.request, setup.value) {
                (0xff, 0x370b) => Ok(vec![PARTNUM_CP2102, 0x00]),
                _ => Err(()),
            };
        }
        if recipient != 1 || setup.index != 0 {
            return Err(());
        }
        let (cts, dsr, dcd) = self.peer_pins(dev);
        let chip = &mut self.chips[dev];
        let v = setup.value;
        let word = |i: usize| u32::from_le_bytes([data[i], data[i + 1], data[i + 2], data[i + 3]]);
        let reply = match setup.request {
            0x00 => {
                chip.enabled = v & 1 != 0;
                vec![]
            }
            0x03 => {
                chip.set_line_ctl(v);
                vec![]
            }
            0x05 => {
                chip.break_on = v & 1 != 0;
                vec![]
            }
            0x07 => {
                if v & 0x100 != 0 {
                    chip.ctl_hs = (chip.ctl_hs & !CTL_HS_DTR_MASK) | (v & 1) as u32;
                }
                if v & 0x200 != 0 {
                    chip.flow_repl &= !FLOW_REPL_RTS_MASK;
                    if v & 2 != 0 {
                        chip.flow_repl |= FLOW_REPL_RTS_ACTIVE;
                    }
                }
                vec![]
            }
            0x08 => vec![
                chip.dtr_out() as u8
                    | (chip.rts_out() as u8) << 1
                    | (cts as u8) << 4
                    | (dsr as u8) << 5
                    | (dcd as u8) << 7,
            ],
            0x10 => {
                let mut hold = 0;
                if chip.cts_handshake() && !cts {
                    hold |= HOLD_CTS;
                }
                if chip.break_on {
                    hold |= HOLD_BREAK;
                }
                let mut r = Vec::with_capacity(19);
                r.extend_from_slice(&chip.errors.to_le_bytes());
                r.extend_from_slice(&hold.to_le_bytes());
                r.extend_from_slice(&(chip.rx.len() as u32).to_le_bytes());
                r.extend_from_slice(&(chip.out_queue() as u32).to_le_bytes());
                r.extend_from_slice(&[0, 0, 0]);
                chip.errors = 0;
                r
            }
            0x12 => {
                if v & 0x5 != 0 {
                    chip.tx.clear();
                }
                if v & 0xa != 0 {
                    chip.rx.clear();
                }
                vec![]
            }
            0x13 => {
                chip.ctl_hs = word(0);
                chip.flow_repl = word(4);
                chip.flow_limits.copy_from_slice(&data[8..16]);
                vec![]
            }
            0x14 => {
                let mut r = Vec::with_capacity(16);
                r.extend_from_slice(&chip.ctl_hs.to_le_bytes());
                r.extend_from_slice(&chip.flow_repl.to_le_bytes());
                r.extend_from_slice(&chip.flow_limits);
                r
            }
            0x15 => {
                chip.embed_events = v != 0;
                vec![]
            }
            0x19 => vec![],
            0x1e => {
                chip.fmt.baud = actual_baud(word(0).max(1));
                vec![]
            }
            _ => return Err(()),
        };
        Ok(reply)
    }

    /// Advances the simulation to `now`: transmits, receives, completes URBs.
    pub fn step(&mut self, now: u64) {
        self.advance(now);
        for chip in &mut self.chips {
            while let Some(pending) = chip.pending_in.front() {
                if chip.rx.is_empty() {
                    break;
                }
                let n = pending.length.min(chip.rx.len());
                let data: Vec<u8> = chip.rx.drain(..n).collect();
                let seqnum = pending.seqnum;
                chip.pending_in.pop_front();
                debug!("[{}] bulk in #{seqnum} done: {n} bytes", chip.serial);
                chip.send(usbip::ret_submit(seqnum, 0, n, &data));
            }
            chip.fill_tx();
        }
    }

    /// Runs both wires up to `now` under the current line state. Called before
    /// every URB so a pin or flow control change only affects bytes after it.
    pub fn advance(&mut self, now: u64) {
        for dev in 0..2 {
            let (cts, _, _) = self.peer_pins(dev);
            let chip = &mut self.chips[dev];
            let line = &mut self.lines[dev];
            let idle = !chip.break_on;
            if line.last_level() != idle {
                line.set_level(now.max(line.last_time()), idle);
                line.idle_mark = line.idle_mark.max(now);
            }
            let blocked = chip.break_on || (chip.cts_handshake() && !cts);
            let mut t = line.busy_until.max(line.idle_mark);
            while t <= now && !blocked {
                let Some(byte) = chip.tx.pop_front() else {
                    break;
                };
                t = line.place_frame(t, byte, &chip.fmt);
                chip.inflight.push_back(t);
            }
            line.busy_until = t;
            if chip.tx.is_empty() || blocked {
                line.idle_mark = line.idle_mark.max(now);
            }
        }

        let mut received = Vec::new();
        for dev in 0..2 {
            let chip = &mut self.chips[dev];
            received.clear();
            chip.receiver
                .poll(&self.lines[1 - dev], &chip.fmt, now, &mut received);
            if chip.enabled {
                for &b in &received {
                    chip.push_rx(b);
                }
            }
            while chip.inflight.front().is_some_and(|&end| end <= now) {
                chip.inflight.pop_front();
            }
            self.lines[1 - dev].prune(chip.receiver.cursor);
        }
    }

    /// Earliest future time at which `step` has something to do.
    pub fn next_wakeup(&self, now: u64) -> Option<u64> {
        let mut next: Option<u64> = None;
        let mut consider = |t: u64| {
            let t = t.max(now + 1);
            if next.is_none_or(|n| t < n) {
                next = Some(t);
            }
        };
        for dev in 0..2 {
            let chip = &self.chips[dev];
            let (cts, _, _) = self.peer_pins(dev);
            if !chip.tx.is_empty() && !chip.break_on && !(chip.cts_handshake() && !cts) {
                consider(self.lines[dev].busy_until);
            }
            if let Some(t) = chip.receiver.next_event(&self.lines[1 - dev], &chip.fmt) {
                consider(t);
            }
            if !chip.pending_out.is_empty() {
                if let Some(&end) = chip.inflight.front() {
                    consider(end);
                }
            }
        }
        next
    }
}
