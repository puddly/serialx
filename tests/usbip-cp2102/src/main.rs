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
const SERIALS: [&str; 2] = ["left", "right"];

/// Emulates a null-modem pair of CP2102 adapters over USB/IP.
#[derive(Parser)]
struct Args {
    /// Address for the USB/IP server. Defaults to an ephemeral port, or 3240 with --serve-only.
    #[arg(long)]
    listen: Option<SocketAddr>,
    /// Only run the USB/IP server; attach with the usbip tool yourself.
    #[arg(long)]
    serve_only: bool,
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
    let listen = args.listen.unwrap_or_else(|| {
        if args.serve_only {
            "127.0.0.1:3240"
        } else {
            "127.0.0.1:0"
        }
        .parse()
        .unwrap()
    });
    let listener = TcpListener::bind(listen).await.unwrap();
    let addr = listener.local_addr().unwrap();
    info!("usbip server on {addr}");

    let server = Server::new(Sim::new(SERIALS));
    let devids = {
        let sim = server.sim.lock().unwrap();
        [sim.devid(0), sim.devid(1)]
    };
    tokio::spawn(server.clone().serve(listener));

    if args.serve_only {
        tokio::signal::ctrl_c().await.unwrap();
        return;
    }

    let (ports, ttys) = tokio::task::spawn_blocking(move || {
        let ports = [
            attach(addr, "1-1", devids[0]),
            attach(addr, "1-2", devids[1]),
        ];
        let ttys = [wait_for_tty(SERIALS[0]), wait_for_tty(SERIALS[1])];
        (ports, ttys)
    })
    .await
    .unwrap();

    println!("{}\n{}", ttys[0], ttys[1]);

    tokio::signal::ctrl_c().await.unwrap();
    for port in ports {
        let _ = fs::write(format!("{VHCI}/detach"), port.to_string());
    }
}
