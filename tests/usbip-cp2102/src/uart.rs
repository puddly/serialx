//! Bit-level serial line model. Transmitters append level transitions to a
//! `Line`; a `Receiver` samples the line at its own bit period, exactly like
//! a UART receiver does, so baud/format mismatches produce the same garbage
//! real hardware would.

use std::collections::VecDeque;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Parity {
    #[default]
    None,
    Odd,
    Even,
    Mark,
    Space,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum StopBits {
    #[default]
    One,
    OneAndHalf,
    Two,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Format {
    pub baud: u32,
    pub data_bits: u8,
    pub parity: Parity,
    pub stop_bits: StopBits,
}

impl Default for Format {
    fn default() -> Self {
        Format {
            baud: 9600,
            data_bits: 8,
            parity: Parity::None,
            stop_bits: StopBits::One,
        }
    }
}

impl Format {
    pub fn bit_ns(&self) -> f64 {
        1e9 / self.baud as f64
    }

    pub fn parity_bit(&self, data: u8) -> Option<bool> {
        let mask = ((1u16 << self.data_bits) - 1) as u8;
        let odd_ones = (data & mask).count_ones() % 2 == 1;
        match self.parity {
            Parity::None => None,
            Parity::Odd => Some(!odd_ones),
            Parity::Even => Some(odd_ones),
            Parity::Mark => Some(true),
            Parity::Space => Some(false),
        }
    }

    fn stop_len(&self) -> f64 {
        match self.stop_bits {
            StopBits::One => 1.0,
            StopBits::OneAndHalf => 1.5,
            StopBits::Two => 2.0,
        }
    }

    /// Bits from the start edge to the sample point of the stop bit.
    fn stop_sample_offset(&self) -> f64 {
        let parity = if self.parity == Parity::None {
            0.0
        } else {
            1.0
        };
        1.5 + self.data_bits as f64 + parity
    }
}

/// One direction of the wire. Level `true` is idle (mark).
#[derive(Debug)]
pub struct Line {
    transitions: VecDeque<(u64, bool)>,
    base_level: bool,
    /// Time at which the last placed frame ends.
    pub busy_until: u64,
    /// Earliest time a new frame may start; advanced to "now" whenever the
    /// transmitter had nothing to send, so an idle gap stays idle.
    pub idle_mark: u64,
}

impl Default for Line {
    fn default() -> Self {
        Line {
            transitions: VecDeque::new(),
            base_level: true,
            busy_until: 0,
            idle_mark: 0,
        }
    }
}

impl Line {
    pub fn level_at(&self, t: u64) -> bool {
        let i = self.transitions.partition_point(|&(tt, _)| tt <= t);
        if i == 0 {
            self.base_level
        } else {
            self.transitions[i - 1].1
        }
    }

    pub fn last_level(&self) -> bool {
        self.transitions.back().map_or(self.base_level, |&(_, l)| l)
    }

    pub fn last_time(&self) -> u64 {
        self.transitions.back().map_or(0, |&(t, _)| t)
    }

    pub fn set_level(&mut self, t: u64, level: bool) {
        assert!(t >= self.last_time(), "transition placed in the past");
        if level != self.last_level() {
            self.transitions.push_back((t, level));
        }
    }

    pub fn next_edge(&self, from: u64, to_level: bool) -> Option<u64> {
        let i = self.transitions.partition_point(|&(tt, _)| tt < from);
        self.transitions
            .range(i..)
            .find(|&&(_, l)| l == to_level)
            .map(|&(t, _)| t)
    }

    /// Places one character frame starting at `t0`; returns the frame end.
    pub fn place_frame(&mut self, t0: u64, byte: u8, fmt: &Format) -> u64 {
        let bit = fmt.bit_ns();
        let mut bits = vec![false];
        for i in 0..fmt.data_bits {
            bits.push((byte >> i) & 1 == 1);
        }
        bits.extend(fmt.parity_bit(byte));
        for (k, &b) in bits.iter().enumerate() {
            self.set_level((t0 as f64 + k as f64 * bit).round() as u64, b);
        }
        let n = bits.len() as f64;
        self.set_level((t0 as f64 + n * bit).round() as u64, true);
        (t0 as f64 + (n + fmt.stop_len()) * bit).round() as u64
    }

    /// Drops transitions no receiver will look at again.
    pub fn prune(&mut self, before: u64) {
        let i = self.transitions.partition_point(|&(tt, _)| tt <= before);
        if i > 1 {
            self.base_level = self.transitions[i - 1].1;
            self.transitions.drain(..i - 1);
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RxByte {
    pub data: u8,
    pub framing: bool,
    pub parity_err: bool,
    pub brk: bool,
}

#[derive(Debug, Default)]
pub struct Receiver {
    pub cursor: u64,
    wait_high: bool,
}

impl Receiver {
    /// Consumes every frame whose stop bit sample time is at or before `now`.
    pub fn poll(&mut self, line: &Line, fmt: &Format, now: u64, out: &mut Vec<RxByte>) {
        let bit = fmt.bit_ns();
        loop {
            if self.wait_high {
                match line.next_edge(self.cursor, true) {
                    Some(t) if t <= now => {
                        self.cursor = t;
                        self.wait_high = false;
                    }
                    _ => return,
                }
            }
            let edge = match line.next_edge(self.cursor, false) {
                Some(t) if t <= now => t,
                _ => return,
            };
            let sample = |k: f64| (edge as f64 + k * bit).round() as u64;
            let t_stop = sample(fmt.stop_sample_offset());
            if t_stop > now {
                return;
            }
            let t_half = sample(0.5);
            if line.level_at(t_half) {
                // Glitch shorter than half a bit: not a start bit.
                self.cursor = t_half;
                continue;
            }
            let mut data = 0u8;
            for k in 0..fmt.data_bits {
                if line.level_at(sample(1.5 + k as f64)) {
                    data |= 1 << k;
                }
            }
            let parity_sample = if fmt.parity == Parity::None {
                None
            } else {
                Some(line.level_at(sample(1.5 + fmt.data_bits as f64)))
            };
            let parity_ok = match (fmt.parity_bit(data), parity_sample) {
                (Some(expected), Some(seen)) => expected == seen,
                _ => true,
            };
            let framing = !line.level_at(t_stop);
            let brk = framing && data == 0 && parity_sample != Some(true);
            out.push(RxByte {
                data,
                framing,
                parity_err: !parity_ok && !brk,
                brk,
            });
            self.cursor = t_stop;
            if framing {
                self.wait_high = true;
            }
        }
    }

    /// Time of the next event `poll` could act on, if one is already on the line.
    pub fn next_event(&self, line: &Line, fmt: &Format) -> Option<u64> {
        if self.wait_high {
            return line.next_edge(self.cursor, true);
        }
        let edge = line.next_edge(self.cursor, false)?;
        Some((edge as f64 + fmt.stop_sample_offset() * fmt.bit_ns()).round() as u64)
    }
}
