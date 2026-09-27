//! USB/IP server: protocol framing plus a per-device connection loop that
//! keeps URBs pending and completes them out of order.

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use log::{debug, info, warn};
use tokio::io::{AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::tcp::OwnedWriteHalf;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc::{unbounded_channel, UnboundedReceiver};
use tokio::sync::Notify;

use crate::cp2102::{Sim, Urb, BCD_DEVICE, PID, VID};

const VERSION: u16 = 0x0111;
const OP_REQ_DEVLIST: u16 = 0x8005;
const OP_REP_DEVLIST: u16 = 0x0005;
const OP_REQ_IMPORT: u16 = 0x8003;
pub const OP_REP_IMPORT: u16 = 0x0003;
const CMD_SUBMIT: u32 = 1;
const CMD_UNLINK: u32 = 2;
const RET_SUBMIT: u32 = 3;
const RET_UNLINK: u32 = 4;
pub const EPIPE: i32 = -32;
const ECONNRESET: i32 = -104;
pub const USB_SPEED_FULL: u32 = 2;
pub const HEADER_LEN: usize = 48;
pub const DEVICE_INFO_LEN: usize = 312;
const FRAME_NS: u64 = 1_000_000;

/// RET_SUBMIT or RET_UNLINK; `data` is the IN payload, empty otherwise.
fn ret(command: u32, seqnum: u32, status: i32, actual_length: usize, data: &[u8]) -> Vec<u8> {
    let mut p = Vec::with_capacity(HEADER_LEN + data.len());
    p.extend_from_slice(&command.to_be_bytes());
    p.extend_from_slice(&seqnum.to_be_bytes());
    p.extend_from_slice(&[0; 12]);
    p.extend_from_slice(&status.to_be_bytes());
    p.extend_from_slice(&(actual_length as u32).to_be_bytes());
    p.extend_from_slice(&[0; 20]);
    p.extend_from_slice(data);
    p
}

pub fn ret_submit(seqnum: u32, status: i32, actual_length: usize, data: &[u8]) -> Vec<u8> {
    ret(RET_SUBMIT, seqnum, status, actual_length, data)
}

fn fixed_str(s: &str, len: usize) -> Vec<u8> {
    let mut v = s.as_bytes().to_vec();
    v.resize(len, 0);
    v
}

fn device_info(sim: &Sim, dev: usize) -> Vec<u8> {
    let chip = &sim.chips[dev];
    let mut p = Vec::with_capacity(DEVICE_INFO_LEN);
    p.extend(fixed_str(
        &format!("/sys/devices/virtual/usbip-cp2102/{}", chip.busid),
        256,
    ));
    p.extend(fixed_str(&chip.busid, 32));
    p.extend_from_slice(&1u32.to_be_bytes());
    p.extend_from_slice(&chip.devnum.to_be_bytes());
    p.extend_from_slice(&USB_SPEED_FULL.to_be_bytes());
    p.extend_from_slice(&VID.to_be_bytes());
    p.extend_from_slice(&PID.to_be_bytes());
    p.extend_from_slice(&BCD_DEVICE.to_be_bytes());
    p.extend_from_slice(&[0, 0, 0, 1, 1, 1]);
    p
}

fn op_header(code: u16, status: u32) -> Vec<u8> {
    let mut p = Vec::new();
    p.extend_from_slice(&VERSION.to_be_bytes());
    p.extend_from_slice(&code.to_be_bytes());
    p.extend_from_slice(&status.to_be_bytes());
    p
}

pub fn op_req_import(busid: &str) -> Vec<u8> {
    let mut p = op_header(OP_REQ_IMPORT, 0);
    p.extend(fixed_str(busid, 32));
    p
}

pub struct Server {
    pub sim: Mutex<Sim>,
    notify: Notify,
    epoch: Instant,
    /// Simulated nanoseconds per wall-clock nanosecond.
    time_scale: f64,
}

impl Server {
    pub fn new(sim: Sim, time_scale: f64) -> Arc<Self> {
        Arc::new(Server {
            sim: Mutex::new(sim),
            notify: Notify::new(),
            epoch: Instant::now(),
            time_scale,
        })
    }

    /// Simulated time now.
    fn now(&self) -> u64 {
        (self.epoch.elapsed().as_nanos() as f64 * self.time_scale) as u64
    }

    /// Wall-clock instant of a simulated time.
    fn wall(&self, t: u64) -> tokio::time::Instant {
        (self.epoch + Duration::from_nanos((t as f64 / self.time_scale) as u64)).into()
    }

    pub async fn serve(self: Arc<Self>, listener: TcpListener) {
        tokio::spawn(self.clone().tick());
        loop {
            let (socket, peer) = listener.accept().await.unwrap();
            debug!("connection from {peer}");
            tokio::spawn(self.clone().connection(socket));
        }
    }

    /// Advances the simulation at most once per full-speed USB frame, so
    /// bulk transfers complete in frame-sized batches like a real host
    /// controller would, instead of once per byte.
    async fn tick(self: Arc<Self>) {
        loop {
            // Read the clock under the lock: `submit` may advance the wires too
            let (now, next) = {
                let mut sim = self.sim.lock().unwrap();
                let now = self.now();
                sim.step(now);
                (now, sim.next_wakeup(now))
            };
            let frame = (FRAME_NS as f64 * self.time_scale) as u64;
            match next {
                Some(t) => {
                    tokio::select! {
                        _ = tokio::time::sleep_until(self.wall(t + frame)) => {}
                        _ = self.notify.notified() => {}
                    }
                }
                None => self.notify.notified().await,
            }
            tokio::time::sleep_until(self.wall(now + frame)).await;
        }
    }

    async fn connection(self: Arc<Self>, socket: TcpStream) {
        socket.set_nodelay(true).unwrap();
        let (rd, wr) = socket.into_split();
        let mut rd = BufReader::new(rd);
        let (tx, rx) = unbounded_channel();
        tokio::spawn(writer(wr, rx));

        let dev = loop {
            let mut hdr = [0u8; 8];
            if rd.read_exact(&mut hdr).await.is_err() {
                return;
            }
            match u16::from_be_bytes([hdr[2], hdr[3]]) {
                OP_REQ_DEVLIST => {
                    let sim = self.sim.lock().unwrap();
                    let mut p = op_header(OP_REP_DEVLIST, 0);
                    p.extend_from_slice(&2u32.to_be_bytes());
                    for dev in 0..2 {
                        p.extend(device_info(&sim, dev));
                        p.extend_from_slice(&[0xff, 0, 0, 0]);
                    }
                    tx.send(p).unwrap();
                }
                OP_REQ_IMPORT => {
                    let mut busid = [0u8; 32];
                    if rd.read_exact(&mut busid).await.is_err() {
                        return;
                    }
                    let end = busid.iter().position(|&b| b == 0).unwrap_or(32);
                    let busid = String::from_utf8_lossy(&busid[..end]).to_string();
                    let mut sim = self.sim.lock().unwrap();
                    match sim.find(&busid) {
                        Some(dev) if sim.chips[dev].reply.is_none() => {
                            let mut p = op_header(OP_REP_IMPORT, 0);
                            p.extend(device_info(&sim, dev));
                            sim.chips[dev].reply = Some(tx.clone());
                            tx.send(p).unwrap();
                            info!("imported {busid}");
                            break dev;
                        }
                        _ => {
                            warn!("import of {busid} refused");
                            tx.send(op_header(OP_REP_IMPORT, 1)).unwrap();
                            return;
                        }
                    }
                }
                code => {
                    warn!("unknown op code {code:#06x}");
                    return;
                }
            }
        };

        loop {
            let mut hdr = [0u8; HEADER_LEN];
            if rd.read_exact(&mut hdr).await.is_err() {
                break;
            }
            let u32_at =
                |i: usize| u32::from_be_bytes([hdr[i], hdr[i + 1], hdr[i + 2], hdr[i + 3]]);
            let seqnum = u32_at(4);
            match u32_at(0) {
                CMD_SUBMIT => {
                    let direction = u32_at(12);
                    let length = u32_at(24);
                    let mut data = vec![0u8; if direction == 0 { length as usize } else { 0 }];
                    if rd.read_exact(&mut data).await.is_err() {
                        break;
                    }
                    let urb = Urb {
                        seqnum,
                        direction,
                        ep: u32_at(16),
                        length,
                        setup: hdr[40..48].try_into().unwrap(),
                        data,
                    };
                    let mut sim = self.sim.lock().unwrap();
                    sim.advance(self.now());
                    sim.submit(dev, urb);
                    drop(sim);
                    self.notify.notify_one();
                }
                CMD_UNLINK => {
                    let found = self.sim.lock().unwrap().unlink(dev, u32_at(20));
                    debug!("[{dev}] unlink #{} found={found}", u32_at(20));
                    let status = if found { ECONNRESET } else { 0 };
                    tx.send(ret(RET_UNLINK, seqnum, status, 0, &[])).unwrap();
                }
                command => {
                    warn!("unknown command {command}");
                    break;
                }
            }
        }
        let mut sim = self.sim.lock().unwrap();
        info!("connection for {} closed", sim.chips[dev].busid);
        sim.detach(dev);
    }
}

async fn writer(mut wr: OwnedWriteHalf, mut rx: UnboundedReceiver<Vec<u8>>) {
    while let Some(packet) = rx.recv().await {
        if wr.write_all(&packet).await.is_err() {
            return;
        }
    }
}
