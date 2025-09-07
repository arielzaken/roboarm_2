#	/** @file home_and_run.py
#	 *	@brief FluidNC/GRBL sequencer with robust recovery + guaranteed file streaming.
#	 *	@details
#	 *		Sequence:
#	 *			1) $HZ
#	 *			2) G90; G0 Z180
#	 *			3) $HA
#	 *			4) $HY
#	 *			5) G90; G0 Y45
#	 *			6) $HZ
#	 *			7) $HX
#	 *			8) G90; G0 X45 Y45 Z45 A0
#	 *			9) Stream user file (with prints: START STREAM/END STREAM)
#	 *		Recovery:
#	 *			- On Alarm anywhere: Ctrl-X → $X → $20=0 → G90, then retry.
#	 *			- On ALARM:9 during a homing step: small relative nudge of that axis, then retry.
#	 */

import sys, time, threading, queue, argparse, os
import serial

_rxq = queue.Queue(maxsize=8192)
_last_state = "Unknown"

def _rx_loop(ser):
	global _last_state
	while True:
		try:
			line = ser.readline().decode(errors='ignore').strip()
			if not line:
				continue
			print(f"<< {line}")
			if line.startswith("<") and ">" in line:
				try:
					state = line[1:line.index("|")] if "|" in line else line[1:-1]
					_last_state = state
				except Exception:
					pass
			_rxq.put(line)
		except Exception:
			break

def send(ser, s):
	ser.write((s + "\r\n").encode())

def wait_ack(timeout_s=12.0):
	deadline = time.time() + timeout_s
	while time.time() < deadline:
		try:
			line = _rxq.get(timeout=0.2).lower()
			if line.startswith("ok") or line.startswith("error") or line.startswith("alarm"):
				return line
		except queue.Empty:
			pass
	return None

def wait_state(ser, targets=("Idle",), timeout_s=30.0, poll_period=0.25):
	deadline = time.time() + timeout_s
	while time.time() < deadline:
		send(ser, "?")
		time.sleep(poll_period)
		if _last_state in targets:
			return True
	return False

def hard_clear(ser):
	ser.write(b"\x18")
	time.sleep(0.3)
	ser.reset_input_buffer()
	send(ser, "$X")
	wait_ack(6.0)
	send(ser, "$20=0")		# disable soft limits at runtime
	wait_ack(6.0)
	send(ser, "G90")		# force absolute mode
	wait_ack(3.0)
	wait_state(ser, targets=("Idle","Run","Hold","Jog","Check"), timeout_s=3.0)

def clear_alarm_if_needed(ser):
	if _last_state == "Alarm":
		print("!! Alarm state detected; hard clear")
		hard_clear(ser)
		wait_state(ser, targets=("Idle",), timeout_s=10.0)

def send_blocking(ser, cmd, ack_timeout=20.0, idle_timeout=60.0, retries=2, force_g90=False):
	for attempt in range(1, retries + 1):
		clear_alarm_if_needed(ser)
		if force_g90:
			send(ser, "G90")
			wait_ack(5.0)
		print(f">> {cmd} (attempt {attempt}/{retries})")
		send(ser, cmd)
		ack = wait_ack(ack_timeout)
		ok_idle = wait_state(ser, targets=("Idle",), timeout_s=idle_timeout)
		if _last_state == "Alarm" or (ack and ack.startswith("alarm")) or not ok_idle:
			print("!! Step did not complete cleanly; recovering (Ctrl-X + $X + $20=0)")
			hard_clear(ser)
			continue
		return True
	return False

def nudge_axis(ser, axis, delta, feed=1000):
	send(ser, "G91")
	wait_ack(3.0)
	send(ser, f"G1 {axis}{delta} F{feed}")
	wait_ack(15.0)
	wait_state(ser, targets=("Idle",), timeout_s=20.0)
	send(ser, "G90")
	wait_ack(3.0)

def axis_from_home_cmd(cmd):
	return cmd[-1].upper() if cmd.startswith("$H") and len(cmd) >= 3 else None

def initial_toward_sign(axis):
	return -1.0		# your config homes toward negative on most axes

def homing_step(ser, cmd, retries=5, nudge_dist=1.0, nudge_feed=1000):
	ax = axis_from_home_cmd(cmd)
	for attempt in range(1, retries + 1):
		print(f">> Homing {cmd} (attempt {attempt}/{retries})")
		clear_alarm_if_needed(ser)
		send(ser, cmd)
		ack = wait_ack(40.0)
		ok_idle = wait_state(ser, targets=("Idle",), timeout_s=120.0)
		if (ack and ack.startswith("ok")) and ok_idle:
			return True
		if ack and ack.startswith("alarm"):
			print(f"!! Homing reported {ack}")
			if ("alarm:9" in ack or "alarm:8" in ack) and ax:
				sign = initial_toward_sign(ax) if attempt == 1 else (1.0 if attempt % 2 == 0 else -1.0)
				print(f"!! ALARM -> nudge {ax} by {sign*nudge_dist} (relative), then retry")
				hard_clear(ser)
				nudge_axis(ser, ax, sign * nudge_dist, feed=nudge_feed)
				continue
			hard_clear(ser)
			continue
		print("!! Homing did not reach Idle; recovering")
		hard_clear(ser)
	return False

def stream_file(ser, path, line_retries=1):
	try:
		size = os.path.getsize(path)
		print(f">> START STREAM: {path} ({size} bytes)")
	except Exception:
		print(f">> START STREAM: {path}")
	# sanity: be Idle and not in Alarm before streaming
	clear_alarm_if_needed(ser)
	wait_state(ser, targets=("Idle",), timeout_s=10.0)
	# preview first non-comment line
	try:
		with open(path, "r", encoding="utf-8", errors="ignore") as pf:
			for raw in pf:
				s = raw.strip()
				if not s or s.startswith("(") or s.startswith(";"):
					continue
				print(f">> first line preview: {s}")
				break
	except Exception as e:
		print("!! Could not open file:", e)
		return False
	# stream
	with open(path, "r", encoding="utf-8", errors="ignore") as f:
		lineno = 0
		for raw in f:
			lineno += 1
			line = raw.strip()
			if not line or line.startswith("(") or line.startswith(";"):
				continue
			for attempt in range(1, line_retries + 2):
				print(f">> [{lineno}] {line} (attempt {attempt}/{line_retries+1})")
				clear_alarm_if_needed(ser)
				send(ser, line)
				ack = wait_ack(20.0)
				if ack is None:
					pass
				elif ack.startswith("ok"):
					break
				elif ack.startswith("alarm"):
					print("!! Alarm during streaming; recovering")
					hard_clear(ser)
					send(ser, "G90")
					wait_ack(5.0)
					if attempt <= line_retries:
						continue
					else:
						print("!! Giving up on this line due to repeated alarms")
						return False
				clear_alarm_if_needed(ser)
				break
	print(">> END STREAM")
	return True

def main():
	ap = argparse.ArgumentParser(description="Self-recovering homing + run-file for FluidNC/GRBL")
	ap.add_argument("port", help="COM port (e.g. COM7 or /dev/ttyUSB0)")
	ap.add_argument("file", help="Path to G-code file to stream after sequence")
	ap.add_argument("--baud", type=int, default=115200)
	ap.add_argument("--home_retries", type=int, default=5)
	ap.add_argument("--line_retries", type=int, default=1)
	ap.add_argument("--nudge", type=float, default=1.0, help="Nudge amount (deg/mm) for ALARM:9 recovery")
	args = ap.parse_args()

	if not os.path.exists(args.file):
		print("File not found:", args.file)
		sys.exit(2)

	print(f">> PORT: {args.port}  BAUD: {args.baud}")
	print(f">> FILE: {args.file}")

	ser = serial.Serial(args.port, args.baud, timeout=0.1, write_timeout=1.0)
	hard_clear(ser)

	t = threading.Thread(target=_rx_loop, args=(ser,), daemon=True)
	t.start()

	# 1) $HZ
	if not homing_step(ser, "$HZ", retries=args.home_retries, nudge_dist=args.nudge):
		print("!! Failed to home Z"); ser.close(); sys.exit(3)

	# 2) G90; G0 Z180
	if not send_blocking(ser, "G0 Z180", ack_timeout=30.0, idle_timeout=60.0, retries=3, force_g90=True):
		print("!! Failed move Z180"); ser.close(); sys.exit(3)

	# 3) $HA
	if not homing_step(ser, "$HA", retries=args.home_retries, nudge_dist=args.nudge):
		print("!! Failed to home A"); ser.close(); sys.exit(3)

	# 4) $HY
	if not homing_step(ser, "$HY", retries=args.home_retries, nudge_dist=args.nudge):
		print("!! Failed to home Y"); ser.close(); sys.exit(3)

	# 5) G90; G0 Y45
	if not send_blocking(ser, "G0 Y45", ack_timeout=30.0, idle_timeout=45.0, retries=3, force_g90=True):
		print("!! Failed move Y45"); ser.close(); sys.exit(3)

	# 6) $HZ
	if not homing_step(ser, "$HZ", retries=args.home_retries, nudge_dist=args.nudge):
		print("!! Failed to home Z (second pass)"); ser.close(); sys.exit(3)

	# 7) $HX
	if not homing_step(ser, "$HX", retries=args.home_retries, nudge_dist=args.nudge):
		print("!! Failed to home X"); ser.close(); sys.exit(3)

	# 8) G90; G0 X45 Y45 Z45 A45
	if not send_blocking(ser, "G0 X45 Y45 Z45 A45", ack_timeout=45.0, idle_timeout=90.0, retries=3, force_g90=True):
		print("!! Failed final pose move"); ser.close(); sys.exit(3)

	# 9) STREAM FILE (always attempted if we reached here)
	ok = stream_file(ser, args.file, line_retries=args.line_retries)
	print(">> Job complete" if ok else "!! Streaming aborted due to persistent alarms")

	ser.close()

if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
		pass
