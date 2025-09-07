#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stdin-driven G-code streamer (GRBL/FluidNC)
- Lines from stdin are queued; each is sent only after an 'ok' is received.
- Macro %%HOME -> runs multi-step homing sequence with waits and retries; ABORTS on failure.
- Macro %%RESET -> sends Ctrl-X soft reset (optional use).
- Handles piped input via EOF sentinel; waits for Idle before exit.
- By default DOES NOT CLOSE the serial port (avoids auto-reset on many ESP32 adapters).
"""

import sys, time, threading, queue, argparse, traceback, re
import serial

# ---------------------- Globals / State ----------------------

_rxq = queue.Queue(maxsize=8192)   # acks: "ok", "error:...", "alarm:..."
_workq = queue.Queue(maxsize=65536)
EOF_SENTINEL = {"type": "eof"}

_last_state = "Unknown"
_stop = threading.Event()

ALARM_RE = re.compile(r'^alarm:?\s*(\d+)', re.IGNORECASE)
POLL_INTERVAL_S = 0.25

# ---------------------- RX thread ----------------------

def _rx_loop(ser: serial.Serial):
	"""Read device lines, print them, push acks to _rxq, track <State|...>."""
	global _last_state
	try:
		while not _stop.is_set():
			raw = ser.readline()
			if not raw:
				continue
			line = raw.decode(errors='ignore').strip()
			print(f"<< {line}")
			# Track <State|...>
			if line.startswith("<"):
				bar = line.find("|")
				if bar > 0:
					_last_state = line[1:bar]
				elif line.endswith(">"):
					_last_state = line[1:-1]
			# Acks into queue (lowercased)
			lower = line.lower()
			if lower.startswith(("ok", "error", "alarm")):
				try:
					_rxq.put(lower, timeout=0.1)
				except queue.Full:
					print("[WARN] RX queue full; dropping ack", file=sys.stderr)
	except Exception:
		traceback.print_exc()

# ---------------------- Status & wait helpers ----------------------

def request_status(ser):
	try:
		ser.write(b"?"); ser.flush()
	except Exception as e:
		print(f"[WARN] status poll failed: {e}", file=sys.stderr)

def wait_until_idle(ser, timeout_s=180.0):
	"""Poll until controller is Idle (double-confirmed fresh)."""
	start = time.time()
	request_status(ser); time.sleep(POLL_INTERVAL_S)
	while (time.time() - start) < timeout_s:
		if _last_state == "Idle":
			time.sleep(POLL_INTERVAL_S); request_status(ser); time.sleep(0.02)
			if _last_state == "Idle":
				return True
		else:
			time.sleep(POLL_INTERVAL_S); request_status(ser)
	print(f"[TIMEOUT] not Idle after {timeout_s}s (last='{_last_state}')", file=sys.stderr)
	return False

def wait_motion_complete(ser, leave_idle_timeout=2.0, finish_timeout=300.0):
	"""Observe leaving Idle then wait to return to Idle."""
	start = time.time()
	request_status(ser)
	while (time.time() - start) < leave_idle_timeout:
		if _last_state != "Idle":
			break
		time.sleep(POLL_INTERVAL_S); request_status(ser)
	return wait_until_idle(ser, timeout_s=finish_timeout)

# ---------------------- TX primitives ----------------------

def send_line(ser: serial.Serial, s: str):
	if not s.endswith("\n"): s += "\n"
	ser.write(s.encode("utf-8")); ser.flush()

def wait_ack(timeout_s=12.0):
	deadline = time.time() + timeout_s
	while time.time() < deadline:
		try:
			return _rxq.get(timeout=0.25)  # lowercased
		except queue.Empty:
			pass
	return None

def send_gcode(ser: serial.Serial, gcode: str, retries=1, ack_timeout=12.0):
	for attempt in range(1, retries + 1):
		print(f">> {gcode}  (try {attempt}/{retries})")
		send_line(ser, gcode)
		ack = wait_ack(timeout_s=ack_timeout)
		if ack is None:
			print(f"[TIMEOUT] no reply for: {gcode}", file=sys.stderr)
			continue
		if ack.startswith("ok"):
			return True, ack
		print(f"[FW] {ack}  for: {gcode}", file=sys.stderr)
		if attempt < retries:
			time.sleep(0.2)
	return False, ack if ack else "timeout"

def parse_alarm_code(ack_line: str):
	m = ALARM_RE.match(ack_line or "")
	if not m:
		return None
	try:
		return int(m.group(1))
	except ValueError:
		return None

def unlock_after_alarm(ser: serial.Serial):
	send_line(ser, "$X")
	time.sleep(0.2)
	# drop any immediate msgs so next wait_ack sees the right thing
	while True:
		try:
			_rxq.get_nowait()
		except queue.Empty:
			break

def send_homing_with_retries(ser: serial.Serial, homing_cmd: str, max_tries=5, ack_timeout=20.0):
	"""
	Send a homing command; retry on ALARM 8/9 with $X; other errors fail.
	"""
	for i in range(1, max_tries + 1):
		ok, ack = send_gcode(ser, homing_cmd, retries=1, ack_timeout=ack_timeout)
		if ok:
			return True
		alarm = parse_alarm_code(ack)
		if alarm in (8, 9):
			print(f"[INFO] Homing alarm {alarm} on try {i}/{max_tries}. Unlock + retry …", file=sys.stderr)
			unlock_after_alarm(ser); time.sleep(0.5); continue
		print(f"[ERR] Non-recoverable response during homing: {ack}", file=sys.stderr)
		return False
	print(f"[ERR] Homing failed after {max_tries} tries.", file=sys.stderr)
	return False

# ---------------------- Macros & sender loop ----------------------

def _perform_home_sequence(ser: serial.Serial, homing_retries: int) -> bool:
	"""
	Exact sequence you requested, with waits between each step.
	Abort on first failure.
	"""
	steps = [
		lambda: send_homing_with_retries(ser, "$HZ", max_tries=homing_retries),
		lambda: wait_motion_complete(ser),
		lambda: send_gcode(ser, "g0 z180")[0],
		lambda: wait_motion_complete(ser),

		lambda: send_homing_with_retries(ser, "$HA", max_tries=homing_retries),
		lambda: wait_motion_complete(ser),

		lambda: send_homing_with_retries(ser, "$HY", max_tries=homing_retries),
		lambda: wait_motion_complete(ser),

		lambda: send_gcode(ser, "g0 y45")[0],
		lambda: wait_motion_complete(ser),

		lambda: send_homing_with_retries(ser, "$HZ", max_tries=homing_retries),
		lambda: wait_motion_complete(ser),

		lambda: send_homing_with_retries(ser, "$HX", max_tries=homing_retries),
		lambda: wait_motion_complete(ser),

		lambda: send_gcode(ser, "g0 x45 y45 z45 a45")[0],
		lambda: wait_motion_complete(ser),
	]
	for step in steps:
		if not step():
			print("[ERR] Homing sequence aborted.", file=sys.stderr)
			return False
	return True

def _sender_loop(ser: serial.Serial, homing_retries: int, ack_timeout: float, line_retries: int):
	"""
	Consumes _workq:
	- {"type":"gcode","line":...} -> send & wait ok
	- {"type":"home"} -> run homing sequence (abort on failure)
	- EOF_SENTINEL -> wait for Idle, then exit
	"""
	while True:
		try:
			item = _workq.get(timeout=0.1)
		except queue.Empty:
			if _stop.is_set():
				return
			continue

		if item is EOF_SENTINEL:
			# No more input; ensure motion finished before exit
			wait_motion_complete(ser, leave_idle_timeout=1.0, finish_timeout=600.0)
			return

		if item["type"] == "home":
			print(">> %%HOME (begin sequence)")
			ok = _perform_home_sequence(ser, homing_retries)
			if ok:
				print(">> %%HOME (done)")
			else:
				print(">> %%HOME (FAILED) — ABORTING", file=sys.stderr)
				_stop.set()
				return
			continue

		if item["type"] == "reset":
			print(">> %%RESET (Ctrl-X soft reset)")
			try:
				ser.write(b'\x18'); ser.flush()
			except Exception as e:
				print(f"[ERR] failed to send reset: {e}", file=sys.stderr)
			continue

		# normal G-code
		line = item["line"]
		ok, ack = send_gcode(ser, line, retries=line_retries, ack_timeout=ack_timeout)
		if not ok:
			print(f"[ERR] giving up on line: {line}  (last: {ack})", file=sys.stderr)

# ---------------------- Session ----------------------

def wake_and_sync(ser: serial.Serial):
	ser.reset_input_buffer(); ser.reset_output_buffer()
	ser.write(b"\r\n\r\n"); ser.flush()
	time.sleep(1.5)
	ser.reset_input_buffer()

# ---------------------- Main ----------------------

def main():
	ap = argparse.ArgumentParser(description="stdin-driven G-code streamer with %%HOME/%%RESET macros")
	ap.add_argument("--port", default="COM7")
	ap.add_argument("--baud", type=int, default=115200)
	ap.add_argument("--no-wake", action="store_true")
	ap.add_argument("--homing-retries", type=int, default=5)
	ap.add_argument("--ack-timeout", type=float, default=12.0)
	ap.add_argument("--line-retries", type=int, default=1)
	ap.add_argument("--close-on-exit", action="store_true",
	                help="Close serial on exit (may auto-reset some boards).")
	args = ap.parse_args()

	ser = serial.Serial(args.port, args.baud, timeout=0.1, write_timeout=1.0,
	                    rtscts=False, dsrdtr=False)
	# Keep handshake lines steady to avoid surprise resets
	try:
		ser.dtr = True
		ser.rts = True
	except Exception:
		pass

	if not args.no_wake:
		wake_and_sync(ser)

	# Start threads
	rx_t = threading.Thread(target=_rx_loop, args=(ser,), daemon=True)
	tx_t = threading.Thread(target=_sender_loop,
	                        args=(ser, args.homing_retries, args.ack_timeout, args.line_retries),
	                        daemon=True)
	rx_t.start(); tx_t.start()

	# Producer: stdin → work queue
	try:
		for raw in sys.stdin:
			if _stop.is_set():
				break
			line = raw.strip()
			if not line:
				continue
			if line == "%%HOME":
				_workq.put({"type":"home"})
			elif line == "%%RESET":
				_workq.put({"type":"reset"})
			else:
				_workq.put({"type":"gcode", "line": line})
	finally:
		# Signal end of input; let TX drain and exit
		_workq.put(EOF_SENTINEL)
		tx_t.join()

		# Do NOT close the port by default (prevents auto-reset).
		if args.close_on_exit:
			_stop.set()
			rx_t.join(timeout=1.0)
			try:
				# Keep lines steady during close to minimize chance of reset
				try:
					ser.dtr = True; ser.rts = True
				except Exception:
					pass
				ser.close()
			except Exception:
				pass

if __name__ == "__main__":
	main()
