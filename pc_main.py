import os
import csv
import time
import json
from datetime import datetime
import cv2
import serial
import serial.tools.list_ports
from PIL import Image
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

SERIAL_PORT = None
BAUD_RATE = 115200
CAMERA_INDEX = 1

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL_NAME = "gemini-3.5-flash-lite"

DATA_DIR = "data"
LOG_FILE_PATH = "sorting_log.csv"
SUMMARY_INTERVAL = 20

INCLUDE_TIMESTAMP_IN_LOG = True
location = "TTC Floor 2 | Bin 12"

USER_RECYCLE_SIDE = "LEFT"
USER_TRASH_SIDE = "RIGHT"

stats = {
    "Recycled correctly": 0,
    "Recycled incorrectly": 0,
    "Trashed correctly": 0,
    "Trashed incorrectly": 0,
}

monthly_stats = {}
total_items_processed = 0

os.makedirs(DATA_DIR, exist_ok=True)

if not os.path.exists(LOG_FILE_PATH):
    with open(LOG_FILE_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Item", "Outcome", "Month", "Date", "Time", "Building_Floor", "Bin", "Image_Path"])

SYSTEM_PROMPT = """
You are an automated smart-bin vision classifier looking down at a top platform.
1. Identify the primary object placed on the platform.
2. Determine which side of the image frame the object is placed on ('LEFT' or 'RIGHT') to see what the user intended:
   - Placing on the LEFT side means the user is attempting to RECYCLE the item.
   - Placing on the RIGHT side means the user is attempting to TRASH the item.
3. Decide the true sorting action ('RECYCLE' or 'TRASH') based on these rules:

Route to RECYCLE:
- Clean aluminum cans, tin foil, metal bottles
- Cardboard, paper, flyers, newspapers, magazines
- Hard plastic bottles, jugs, cups, and rigid plastic containers
- Glass bottles and jars
- Soft plastics / plastic bags (NOTE: remove this line if your bin rejects soft plastics!)

Route to TRASH:
- Food waste, dirty paper towels, used napkins
- Polystyrene / Styrofoam cups or takeout clam-shells
- Disposable wax/plastic-lined coffee cups and soiled disposable plates
- Batteries, electronics, shoes, clothing, hands, or non-recyclable clutter
- If the platform is empty or only shows a background/hand, choose TRASH.
"""

class BinDecision(BaseModel):
    detected_item: str = Field(description="Clean, short name of the object (e.g., 'Shoe/clothing', 'Aluminum can', 'Paper towel')")
    material_category: str = Field(description="Closest material category (e.g. aluminium, hard plastic, soft plastics, paper, food/trash, other)")
    placement_side: str = Field(description="Which half of the frame the object is primarily located in: must be 'LEFT' or 'RIGHT'")
    reasoning: str = Field(description="One brief sentence explaining why it is recyclable or trash")
    action: str = Field(description="The true correct bin for this object: must be 'RECYCLE' or 'TRASH'")

def find_pico_port():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        if port.vid == 0x2E8A or "Pico" in port.description or "Board CDC" in port.description:
            return port.device
    raise RuntimeError("Raspberry Pi Pico not found. Set SERIAL_PORT manually.")

port_name = SERIAL_PORT if SERIAL_PORT else find_pico_port()
print(f"[PC-INIT] Connecting to Pico on {port_name}...")
ser = serial.Serial(port_name, BAUD_RATE, timeout=0.1)
time.sleep(2)
ser.reset_input_buffer()

print("[PC-INIT] Opening top-mounted webcam...")
cap = cv2.VideoCapture(CAMERA_INDEX)
if not cap.isOpened():
    raise RuntimeError(f"Could not open webcam at index {CAMERA_INDEX}.")

print(f"[PC-INIT] Initializing {MODEL_NAME} Vision LLM...")
client = genai.Client(api_key=GEMINI_API_KEY)
print(f"[PC-INIT] Ready! Saving images to '{DATA_DIR}/' and logging to '{LOG_FILE_PATH}'...\n")

def capture_frame():
    """Flushes camera buffer so we get the live frame after the hand pulls away."""
    for _ in range(5):
        cap.grab()
    ret, frame = cap.read()
    if not ret:
        raise RuntimeError("Failed to read frame from webcam.")
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb_frame)

def save_classified_image(pil_image, item_name: str, now: datetime) -> str:
    """Saves the captured image into the 'data' folder with a clean filename."""
    safe_item = "".join(c if c.isalnum() else "_" for c in item_name).strip("_").lower()
    filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_item}.jpg"
    filepath = os.path.join(DATA_DIR, filename)
    pil_image.save(filepath, "JPEG")
    return filepath

def evaluate_sorting_outcome(placement_side: str, true_action: str) -> str:
    """
    Compares where the user placed the item (LEFT=Recycle attempt, RIGHT=Trash attempt)
    against the VLM's true classification (RECYCLE vs TRASH).
    """
    user_tried_recycle = (placement_side == USER_RECYCLE_SIDE)

    if user_tried_recycle and true_action == "RECYCLE":
        return "Recycled correctly"
    elif user_tried_recycle and true_action == "TRASH":
        return "Recycled incorrectly"
    elif not user_tried_recycle and true_action == "TRASH":
        return "Trashed correctly"
    else:
        return "Trashed incorrectly"

def record_and_log_item(item_name: str, outcome_status: str, pil_image):
    """
    Saves the image to 'data/', appends the structured row to sorting_log.csv,
    and prints the pipe-delimited string and 20-item summaries.
    """
    global total_items_processed
    now = datetime.now()
    month_label = now.strftime("%b %Y")
    month_short = now.strftime("%b")
    date_str = now.strftime("%m/%d/%Y")
    time_str = now.strftime("%H:%M:%S")
    full_timestamp = f"{date_str} {time_str}"

    image_path = save_classified_image(pil_image, item_name, now)

    stats[outcome_status] += 1
    if month_label not in monthly_stats:
        monthly_stats[month_label] = {
            "Recycled correctly": 0,
            "Recycled incorrectly": 0,
            "Trashed correctly": 0,
            "Trashed incorrectly": 0,
        }
    monthly_stats[month_label][outcome_status] += 1
    total_items_processed += 1

    if INCLUDE_TIMESTAMP_IN_LOG:
        log_entry = f"{item_name} | {outcome_status} | {month_short} | {date_str} | {time_str} | {location}"
    else:
        log_entry = f"{item_name} | {outcome_status} | {location}"

    print(f"  [LOG-ENTRY #{total_items_processed}] {log_entry}")
    print(f"  [IMAGE-SAVED] {image_path}")

    loc_parts = [part.strip() for part in location.split("|")]
    building_floor = loc_parts[0] if len(loc_parts) > 0 else location
    bin_id = loc_parts[1] if len(loc_parts) > 1 else ""

    with open(LOG_FILE_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            item_name,
            outcome_status,
            month_short,
            date_str,
            time_str,
            building_floor,
            bin_id,
            image_path,
        ])
        f.flush()

    if total_items_processed % SUMMARY_INTERVAL == 0:
        monthly_lines = []
        for m_key, m_counts in monthly_stats.items():
            monthly_lines.append(
                f"  [{m_key}] Recycled OK: {m_counts['Recycled correctly']} | "
                f"Recycled Bad: {m_counts['Recycled incorrectly']} | "
                f"Trashed OK: {m_counts['Trashed correctly']} | "
                f"Trashed Bad: {m_counts['Trashed incorrectly']}"
            )
        monthly_breakdown_str = "\n".join(monthly_lines)

        summary_block = (
            f"\n--- RUNNING COUNT AFTER {total_items_processed} ITEMS ({full_timestamp}) ---\n"
            f"Recycled correctly:     {stats['Recycled correctly']}\n"
            f"Recycled incorrectly:   {stats['Recycled incorrectly']}\n"
            f"Trashed correctly:      {stats['Trashed correctly']}\n"
            f"Trashed incorrectly: {stats['Trashed incorrectly']}\n"
            f"By Month (Nov / Dec / Jan...):\n"
            f"{monthly_breakdown_str}\n"
            f"---------------------------------------------------------------\n"
        )
        print(summary_block)

def classify_with_vlm(pil_image):
    """Sends the webcam image to Gemini and returns the structured decision."""
    vlm_img = pil_image.copy()
    vlm_img.thumbnail((512, 512))

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[vlm_img, "Classify the object on the sorting platform and detect whether it is on the LEFT or RIGHT side."],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=BinDecision,
            temperature=0.0,
        ),
    )

    result = json.loads(response.text)
    action = result.get("action", "TRASH").upper()
    if action not in ("RECYCLE", "TRASH"):
        action = "TRASH"

    placement_side = result.get("placement_side", USER_TRASH_SIDE).upper()
    if placement_side not in ("LEFT", "RIGHT"):
        placement_side = USER_TRASH_SIDE

    return result, action, placement_side

def main():
    try:
        while True:
            if ser.in_waiting > 0:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                if line.startswith("[DEBUG"):
                    print(f"PICO -> {line}")

                elif line == "TRIGGER":
                    print("\n==========================================")
                    print("[PC-EVENT] TRIGGER received! Capturing top-down frame...")
                    t0 = time.time()
                    try:
                        image = capture_frame()
                        result, decision, placement_side = classify_with_vlm(image)
                        dt = (time.time() - t0) * 1000

                        detected_item = result.get("detected_item", "Unknown item")
                        material = result.get("material_category", "other")
                        outcome_status = evaluate_sorting_outcome(placement_side, decision)

                        print(f"  [VLM-DETECTED]  {detected_item} ({material}) | Placed on: {placement_side}")
                        print(f"  [VLM-REASONING] {result.get('reasoning')}")

                        record_and_log_item(detected_item, outcome_status, image)

                        print(f"[PC-RESULT] Servo Action: '{decision}' in {dt:.0f}ms -> Sending to Pico")
                    except Exception as e:
                        print(f"[PC-ERROR] VLM inference failed ({e}). Defaulting to TRASH.")
                        decision = "TRASH"

                    print("==========================================\n")
                    ser.write(f"{decision}\n".encode("utf-8"))
                    ser.flush()

            time.sleep(0.01)
    except KeyboardInterrupt:
        print("\n[PC-EXIT] Stopping vision script...")
        print(f"Final Counts ({total_items_processed} total): {stats}")
    finally:
        cap.release()
        ser.close()

if __name__ == "__main__":
    main()