#!/bin/sh
# Builds usbserial.ko and cp210x.ko for the running kernel from the matching
# upstream sources, into ./modules. GitHub's Azure kernels ship no USB serial
# drivers at all, so the emulated CP2102 has nothing to bind to otherwise.
set -eu

out="$(cd "$(dirname "$0")" && pwd)/modules"
release="$(uname -r)"
version="${release%%[-+]*}"
version="${version%.0}"
build=/tmp/cp210x-modules
base="https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain/drivers/usb/serial"

sudo apt-get install -y "linux-headers-$release"
rm -rf "$build"
mkdir -p "$build" "$out"
for f in usb-serial.c generic.c bus.c cp210x.c; do
    curl -sfL "$base/$f?h=v$version" -o "$build/$f" || curl -sfL "$base/$f?h=v${version%.*}" -o "$build/$f"
done
printf 'obj-m += usbserial.o cp210x.o\nusbserial-y := usb-serial.o generic.o bus.o\n' > "$build/Kbuild"
make -C "/lib/modules/$release/build" M="$build" modules
cp "$build/usbserial.ko" "$build/cp210x.ko" "$out/"
