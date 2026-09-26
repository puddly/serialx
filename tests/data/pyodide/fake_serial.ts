// In-memory Web-Serial-shaped port pair for testing Pyodide JS interop.
// Types (SerialOptions, SerialInputSignals, etc) come from @types/w3c-web-serial.

export class FakeSerialPort {
  #peer: FakeSerialPort | null = null;
  #open = false;
  #options: SerialOptions | null = null;
  // Writers stalled on hardware flow control, woken by a peer RTS change or close
  #ctsWaiters: Array<() => void> = [];
  #readable: ReadableStream<Uint8Array> | null = null;
  #writable: WritableStream<Uint8Array> | null = null;
  #inboundController: ReadableStreamDefaultController<Uint8Array> | null = null;
  #out: Required<SerialOutputSignals> = {
    requestToSend: false,
    dataTerminalReady: false,
    break: false,
  };

  static link(a: FakeSerialPort, b: FakeSerialPort): void {
    a.#peer = b;
    b.#peer = a;
  }

  get readable(): ReadableStream<Uint8Array> | null {
    return this.#readable;
  }

  get writable(): WritableStream<Uint8Array> | null {
    return this.#writable;
  }

  #ctsHeld(): boolean {
    if (this.#options?.flowControl !== "hardware") return false;
    const peer = this.#peer;
    return peer === null || !peer.#out.requestToSend;
  }

  #wakeCtsWaiters(): void {
    const waiters = this.#ctsWaiters;
    this.#ctsWaiters = [];
    for (const wake of waiters) wake();
  }

  async open(options: SerialOptions): Promise<void> {
    if (this.#open) {
      throw new Error("FakeSerialPort is already open");
    }
    this.#open = true;
    this.#options = options;

    this.#readable = new ReadableStream<Uint8Array>({
      start: (controller) => {
        this.#inboundController = controller;
      },
    });

    this.#writable = new WritableStream<Uint8Array>({
      write: async (chunk) => {
        const bytes = chunk.slice();
        while (this.#open && this.#ctsHeld()) {
          await new Promise<void>((resolve) => this.#ctsWaiters.push(resolve));
        }
        if (!this.#open) return;
        const peer = this.#peer;
        if (peer && peer.#inboundController) {
          peer.#inboundController.enqueue(bytes);
        }
      },
    });
  }

  async close(): Promise<void> {
    if (!this.#open) return;
    this.#open = false;

    if (this.#inboundController) {
      try {
        this.#inboundController.close();
      } catch {}
      this.#inboundController = null;
    }

    this.#readable = null;
    this.#writable = null;
    this.#options = null;
    this.#wakeCtsWaiters();
  }

  async setSignals(signals: SerialOutputSignals = {}): Promise<void> {
    if ("requestToSend" in signals && signals.requestToSend !== undefined) {
      this.#out.requestToSend = !!signals.requestToSend;
      if (this.#peer !== null) this.#peer.#wakeCtsWaiters();
    }
    if (
      "dataTerminalReady" in signals &&
      signals.dataTerminalReady !== undefined
    ) {
      this.#out.dataTerminalReady = !!signals.dataTerminalReady;
    }
    if ("break" in signals && signals.break !== undefined) {
      this.#out.break = !!signals.break;
    }
  }

  async getSignals(): Promise<SerialInputSignals> {
    const peerOut = this.#peer ? this.#peer.#out : null;
    return {
      clearToSend: !!peerOut?.requestToSend,
      dataCarrierDetect: !!peerOut?.dataTerminalReady,
      dataSetReady: !!peerOut?.dataTerminalReady,
      ringIndicator: false,
    };
  }
}

export function createFakeSerialPair(): [FakeSerialPort, FakeSerialPort] {
  const left = new FakeSerialPort();
  const right = new FakeSerialPort();
  FakeSerialPort.link(left, right);
  return [left, right];
}
