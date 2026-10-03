mod cp2102;
mod uart;
mod usbip;

use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::os::fd::AsRawFd;
use std::thread::sleep;
use std::time::{Duration, Instant};

use clap::Parser;
use log::info;
use tokio::net::TcpListener;

use crate::cp2102::Sim;
use crate::usbip::{op_req_import, Server, DEVICE_INFO_LEN, OP_REP_IMPORT, USB_SPEED_FULL};

const VHCI: &str = "/sys/devices/platform/vhci_hcd.0";

/// Emulates null-modem pairs of CP2102 adapters over USB/IP.
#[derive(Parser)]
struct Args {
    /// Number of null-modem pairs to emulate. vhci_hcd has 8 high-speed ports, so at most 4.
    /// Each pair is its own simulation behind its own USB/IP server, so pairs never
    /// perturb each other's timing.
    #[arg(long, default_value_t = 1)]
    pairs: usize,
    /// Address for the first pair's USB/IP server; later pairs use the following ports.
    /// Defaults to ephemeral ports, or 3240 with --serve-only.
    #[arg(long)]
    listen: Option<SocketAddr>,
    /// Only run the USB/IP server; attach with the usbip tool yourself.
    #[arg(long)]
    serve_only: bool,
    /// Run the wire faster than real time by this factor.
    #[arg(long, default_value_t = 1.0)]
    time_scale: f64,
}

fn free_port() -> u32 {
    let status = fs::read_to_string(format!("{VHCI}/status")).unwrap();
    for line in status.lines().skip(1) {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields[0] == "hs" && fields[2] == "004" {
            return fields[1].parse().unwrap();
        }
    }
    panic!("no free vhci port");
}

/// Imports `busid` from our own server and hands the socket to vhci_hcd.
fn attach(server: SocketAddr, busid: &str, devid: u32) -> u32 {
    let mut s = TcpStream::connect(server).unwrap();
    s.write_all(&op_req_import(busid)).unwrap();
    let mut rep = [0u8; 8 + DEVICE_INFO_LEN];
    s.read_exact(&mut rep).unwrap();
    assert_eq!(u16::from_be_bytes([rep[2], rep[3]]), OP_REP_IMPORT);
    assert_eq!(
        u32::from_be_bytes([rep[4], rep[5], rep[6], rep[7]]),
        0,
        "import refused"
    );
    let port = free_port();
    let cmd = format!("{port} {} {devid} {USB_SPEED_FULL}", s.as_raw_fd());
    fs::write(format!("{VHCI}/attach"), cmd).unwrap();
    port
}

fn find_tty(serial: &str) -> Option<String> {
    for entry in fs::read_dir("/sys/class/tty").unwrap() {
        let name = entry.unwrap().file_name().into_string().unwrap();
        let found = fs::read_to_string(format!("/sys/class/tty/{name}/device/../../serial"));
        if name.starts_with("ttyUSB") && found.is_ok_and(|s| s.trim() == serial) {
            return Some(format!("/dev/{name}"));
        }
    }
    None
}

fn wait_for_tty(serial: &str) -> String {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(tty) = find_tty(serial) {
            return tty;
        }
        assert!(
            Instant::now() < deadline,
            "tty for serial {serial} did not appear"
        );
        sleep(Duration::from_millis(50));
    }
}

#[tokio::main]
async fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let args = Args::parse();
    let base = args.listen.unwrap_or_else(|| {
        if args.serve_only {
            "127.0.0.1:3240"
        } else {
            "127.0.0.1:0"
        }
        .parse()
        .unwrap()
    });

    // (server address, busid, devid, serial) for every emulated chip
    let mut chips: Vec<(SocketAddr, String, u32, String)> = Vec::new();

    for pair in 0..args.pairs {
        let port = if base.port() == 0 {
            0
        } else {
            base.port() + pair as u16
        };
        let listener = TcpListener::bind(SocketAddr::new(base.ip(), port))
            .await
            .unwrap();
        let addr = listener.local_addr().unwrap();
        info!("usbip server for pair {pair} on {addr}");

        let server = Server::new(Sim::new(pair), args.time_scale);
        {
            let sim = server.sim.lock().unwrap();
            for (i, chip) in sim.chips.iter().enumerate() {
                chips.push((addr, chip.busid.clone(), sim.devid(i), chip.serial.clone()));
            }
        }
        tokio::spawn(server.clone().serve(listener));
    }

    if args.serve_only {
        tokio::signal::ctrl_c().await.unwrap();
        return;
    }

    let (ports, ttys) = tokio::task::spawn_blocking(move || {
        let ports: Vec<u32> = chips
            .iter()
            .map(|(addr, busid, devid, _)| attach(*addr, busid, *devid))
            .collect();
        let ttys: Vec<String> = chips
            .iter()
            .map(|(_, _, _, serial)| wait_for_tty(serial))
            .collect();
        (ports, ttys)
    })
    .await
    .unwrap();

    for tty in &ttys {
        println!("{tty}");
    }

    tokio::signal::ctrl_c().await.unwrap();
    for port in ports {
        let _ = fs::write(format!("{VHCI}/detach"), port.to_string());
    }
}
