import sys
import time
import uselect
from machine import Pin, PWM, time_pulse_us

PIR_OUT_PIN = 16
TRIG_PIN = 14
ECHO_PIN = 15
SERVO_PIN = 18

DISTANCE_THRESHOLD_INCHES = 4.0

SERVO_NEUTRAL_ANGLE = 90
SERVO_TRASH_LEFT = 0
SERVO_RECYCLE_RIGHT = 180

DUMP_HOLD_SECONDS = 2.0
DEBUG_INTERVAL_MS = 500

pir = Pin(PIR_OUT_PIN, Pin.IN, Pin.PULL_DOWN)
trig = Pin(TRIG_PIN, Pin.OUT)
echo = Pin(ECHO_PIN, Pin.IN)

servo = PWM(Pin(SERVO_PIN))
servo.freq(50)

current_servo_state = "UNKNOWN"

poller = uselect.poll()
poller.register(sys.stdin, uselect.POLLIN)

def set_servo_angle(angle, label=""):
    """Maps 0-180 degrees to 16-bit PWM duty cycle (0.5ms to 2.5ms pulse)."""
    global current_servo_state
    min_duty = 1638
    max_duty = 8192
    duty = int(min_duty + (angle / 180.0) * (max_duty - min_duty))
    servo.duty_u16(duty)
    current_servo_state = f"{label} ({angle} deg)" if label else f"{angle} deg"
    print(f"[DEBUG-SERVO] Moved to {angle} deg | PWM Duty: {duty} | State: {current_servo_state}")

def get_distance_inches():
    """Pulses HC-SR04 Trig and returns (distance_in_inches, raw_duration_us)."""
    trig.low()
    time.sleep_us(2)
    trig.high()
    time.sleep_us(10)
    trig.low()

    duration = time_pulse_us(echo, 1, 30000)
    if duration < 0:
        return 999.0, duration

    distance_in = (duration * 0.0135039) / 2.0
    return distance_in, duration

def wait_for_pc_decision(timeout_sec=12.0):
    """Reads USB serial until 'RECYCLE' or 'TRASH' is received from the PC."""
    print(f"[DEBUG-SERIAL] Waiting up to {timeout_sec}s for VLM classification...")
    start_time = time.time()
    buffer = ""
    while (time.time() - start_time) < timeout_sec:
        if poller.poll(100):
            char = sys.stdin.read(1)
            if char == "\n":
                command = buffer.strip()
                print(f"[DEBUG-SERIAL] Received command from PC: '{command}'")
                if command in ("RECYCLE", "TRASH"):
                    return command
                buffer = ""
            else:
                buffer += char
    print("[DEBUG-SERIAL] Timed out waiting for PC! Defaulting to TRASH (Full Left).")
    return "TRASH"

print("[DEBUG-BOOT] Initializing Top-Chute Pico Controller...")
set_servo_angle(SERVO_NEUTRAL_ANGLE, "NEUTRAL/CENTER")
time.sleep(0.5)

last_debug_time = time.ticks_ms()

while True:
    pir_val = pir.value()
    dist_in, raw_us = get_distance_inches()

    now = time.ticks_ms()
    if time.ticks_diff(now, last_debug_time) >= DEBUG_INTERVAL_MS:
        pir_status = "MOTION (1)" if pir_val == 1 else "IDLE (0)"
        dist_str = f"{dist_in:.2f} in ({raw_us} us)" if dist_in < 900 else f"OUT_OF_RANGE ({raw_us} us)"
        print(f"[DEBUG-SENSORS] PIR (GP16): {pir_status} | HC-SR04 (GP14/15): {dist_str} | Servo (GP18): {current_servo_state}")
        last_debug_time = now

    if pir_val == 1 and dist_in <= DISTANCE_THRESHOLD_INCHES:
        led.high()
        print(f"[DEBUG-TRIGGER] Object detected | PIR={pir_val}, Dist={dist_in:.2f} in")

        time.sleep(0.4)
        print("TRIGGER")

        decision = wait_for_pc_decision()

        if decision == "RECYCLE":
            print("[DEBUG-ACTION] RECYCLE -> Tilting FULL RIGHT (180 deg)...")
            set_servo_angle(SERVO_RECYCLE_RIGHT, "FULL RIGHT / RECYCLE")
            time.sleep(DUMP_HOLD_SECONDS)
            print("[DEBUG-ACTION] Returning flap to NEUTRAL CENTER (90 deg)...")
            set_servo_angle(SERVO_NEUTRAL_ANGLE, "NEUTRAL/CENTER")
        else:
            print("[DEBUG-ACTION] TRASH -> Tilting FULL LEFT (0 deg)...")
            set_servo_angle(SERVO_TRASH_LEFT, "FULL LEFT / TRASH")
            time.sleep(DUMP_HOLD_SECONDS)
            print("[DEBUG-ACTION] Returning flap to NEUTRAL CENTER (90 deg)...")
            set_servo_angle(SERVO_NEUTRAL_ANGLE, "NEUTRAL/CENTER")

        led.low()
        print("[DEBUG-COOLDOWN] Waiting 2.0s for platform to settle...")
        time.sleep(2.0)
        last_debug_time = time.ticks_ms()

    time.sleep(0.05)