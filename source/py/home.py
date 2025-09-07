#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys, time, threading, queue, argparse, traceback, re
import serial

# ---------------------- RX/State ----------------------

EOF_SENTINEL = {"type": "eof"}

_rxq = queue.Queue(maxsize=8192)   # acks only: "ok", "error:...", "alarm:..."
_last_state = "Unknown"
_stop = threading.Event()

ALARM_RE = re.compile(r'^alarm:?\s*(\d+)', re.IGNORECASE)

def _rx_loop(ser: serial.Serial):
	"""
	Read lines from the controller, print everything to stdout,
	and push acks (lowercased) into _rxq for the sender to consume.
	Also track the <State|...> banner for Idle/Run/Home/Hold.
	"""
	global _last_state
	try:
		while not _stop.is_set():
			try:
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
				# Feed acks into queue (lowercased)
				lower = line.lower()
				if lower.startswith("ok") or lower.startswith("error") or lower.startswith("alarm"):
					try:
						_rxq.put(lower, timeout=0.1)
					except queue.Full:
						print("[WARN] RX queue full; dropping ack", file=sys.stderr)
			except Exception:
				traceback.print_exc()
	except Exception:
		traceback.print_exc()

# ---------------------- Polling helpers ----------------------

POLL_INTERVAL_S = 0.25

def request_status(ser):
	try:
		ser.write(b"?")
		ser.flush()
	except Exception as e:
		print(f"[WARN] status poll failed: {e}", file=sys.stderr)

def wait_until_idle(ser, timeout_s=180.0):
	"""
	Poll until controller reports Idle. Guarantees at least one fresh poll
	AFTER this function begins to avoid returning on a stale 'Idle'.
	"""
	start = time.time()
	request_status(ser)
	time.sleep(POLL_INTERVAL_S)

	while (time.time() - start) < timeout_s:
		if _last_state == "Idle":
			time.sleep(POLL_INTERVAL_S)
			request_status(ser)
			time.sleep(0.02)
			if _last_state == "Idle":
				return True
		else:
			time.sleep(POLL_INTERVAL_S)
			request_status(ser)
	print(f"[TIMEOUT] still not Idle, last state '{_last_state}' after {timeout_s}s", file=sys.stderr)
	return False

def wait_motion_complete(ser, leave_idle_timeout=2.0, finish_timeout=300.0):
	"""
	For motion/homing: first observe a transition away from Idle (Run/Home/Hold),
	then wait until Idle again.
	"""
	start = time.time()
	# phase 1: wait until we SEE non-Idle (or time out)
	request_status(ser)
	while (time.time() - start) < leave_idle_timeout:
		if _last_state != "Idle":
			break
		time.sleep(POLL_INTERVAL_S)
		request_status(ser)
	# phase 2: now wait for completion
	return wait_until_idle(ser, timeout_s=finish_timeout)

# ---------------------- TX/Ack primitives ----------------------

def send_line(ser: serial.Serial, s: str):
	if not s.endswith("\n"):
		s += "\n"
	ser.write(s.encode("utf-8"))
	ser.flush()

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
	"""Return int alarm code or None"""
	m = ALARM_RE.match(ack_line or "")
	if not m:
		return None
	try:
		return int(m.group(1))
	except ValueError:
		return None

def unlock_after_alarm(ser: serial.Serial):
	# Typical GRBL/FluidNC unlock
	send_line(ser, "$X")
	time.sleep(0.2)
	# Flush any backlog of immediate messages
	while True:
		try:
			_rxq.get_nowait()
		except queue.Empty:
			break

def send_homing_with_retries(ser: serial.Serial, homing_cmd: str, max_tries=5, ack_timeout=20.0):
    """
    Start homing by sending the command; consider it 'started' if either:
      - we receive an immediate ack ('ok'/'error'/'alarm'), OR
      - we observe a transition away from Idle within leave_idle_timeout.
    Alarms 8/9 trigger unlock+retry. Others fail.
    """
    leave_idle_timeout = 3.0  # short window to detect motion start
    for i in range(1, max_tries + 1):
        print(f">> {homing_cmd}  (try {i}/{max_tries})")
        send_line(ser, homing_cmd)

        # Wait briefly for an ack OR motion start
        start = time.time()
        started = False
        ack = None
        while (time.time() - start) < leave_idle_timeout:
            try:
                ack = _rxq.get(timeout=0.2)
                if ack.startswith("ok"):
                    started = True
                    break
                if ack.startswith("alarm") or ack.startswith("error"):
                    break
            except queue.Empty:
                pass
            # Poll for status change
            request_status(ser)
            if _last_state != "Idle":
                started = True
                break

        if ack and (ack.startswith("alarm") or ack.startswith("error")) and not started:
            alarm = parse_alarm_code(ack)
            if alarm in (8, 9):
                print(f"[INFO] Homing alarm {alarm} on try {i}/{max_tries}. Unlock + retry …", file=sys.stderr)
                unlock_after_alarm(ser)
                time.sleep(0.5)
                continue
            print(f"[ERR] Non-recoverable response during homing: {ack}", file=sys.stderr)
            return False

        if not started:
            print("[ERR] Homing did not start (no ack / no motion).", file=sys.stderr)
            # Try an unlock in case we were alarmed and missed it
            unlock_after_alarm(ser)
            time.sleep(0.5)
            continue

        # At this point homing is underway; now wait for completion
        if wait_motion_complete(ser, leave_idle_timeout=2.0, finish_timeout=max(ack_timeout, 300.0)):
            return True
        print("[ERR] Homing did not return to Idle.", file=sys.stderr)
        # fall through to retry

    print(f"[ERR] Homing failed after {max_tries} tries.", file=sys.stderr)
    return False


# ---------------------- Streamer (stdin → queue → sender) ----------------------

# Outbound work items:
# - {"type":"gcode", "line": "G0 X10"}
# - {"type":"home"}  (special)
_workq = queue.Queue(maxsize=65536)

def _perform_home_sequence(ser: serial.Serial, homing_retries: int) -> bool:
	"""
	Your exact multi-step homing/train sequence with waits in between.
	Returns True on success, False on first failure.
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
		ok = step()
		if not ok:
			print("[ERR] Homing sequence aborted.", file=sys.stderr)
			return False
	return True

def _sender_loop(ser: serial.Serial, homing_retries: int, ack_timeout: float, line_retries: int):
    while True:
        try:
            item = _workq.get(timeout=0.1)
        except queue.Empty:
            # If stop was requested and queue is empty, exit
            if _stop.is_set():
                return
            continue

        if item is EOF_SENTINEL:
            # No more input; just exit (queue is already empty here)
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

        if item["type"] == "gcode":
            line = item["line"]
            ok, ack = send_gcode(ser, line, retries=line_retries, ack_timeout=ack_timeout)
            if not ok:
                print(f"[ERR] giving up on line: {line}  (last: {ack})", file=sys.stderr)
            continue


# ---------------------- Session/wake ----------------------

def wake_and_sync(ser: serial.Serial):
	ser.reset_input_buffer()
	ser.reset_output_buffer()
	ser.write(b"\r\n\r\n")
	ser.flush()
	time.sleep(1.5)
	ser.reset_input_buffer()

# ---------------------- Main ----------------------

def main():
	parser = argparse.ArgumentParser(description="stdin-driven G-code streamer with %%HOME macro.")
	parser.add_argument("--port", default="COM7")
	parser.add_argument("--baud", type=int, default=115200)
	parser.add_argument("--no-wake", action="store_true")
	parser.add_argument("--homing-retries", type=int, default=5)
	parser.add_argument("--ack-timeout", type=float, default=12.0, help="seconds to wait for OK/error/alarm on each line")
	parser.add_argument("--line-retries", type=int, default=1, help="retries for line-level timeouts")
	args = parser.parse_args()

	ser = serial.Serial(args.port, args.baud, timeout=0.1, write_timeout=1.0)
	if not args.no_wake:
		wake_and_sync(ser)

	# Start threads
	rx_t = threading.Thread(target=_rx_loop, args=(ser,), daemon=True)
	tx_t = threading.Thread(target=_sender_loop, args=(ser, args.homing_retries, args.ack_timeout, args.line_retries), daemon=True)
	rx_t.start()
	tx_t.start()

	# Producer: read stdin and enqueue work
	try:
		for raw in sys.stdin:
			if _stop.is_set():
				break
			line = raw.strip()
			if not line:
				continue
			if line == "%%HOME":
				_workq.put({"type":"home"})
			else:
				_workq.put({"type":"gcode", "line": line})
	finally:
		# Signal no more input, but DON'T stop yet—let TX drain and exit cleanly
		_workq.put(EOF_SENTINEL)
		# Wait for TX to finish everything (no timeout)
		tx_t.join()
		# Now we can stop RX and close
		_stop.set()
		rx_t.join(timeout=1.0)
		try:
			ser.close()
		except Exception:
			pass


if __name__ == "__main__":
	main()
