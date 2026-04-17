#!/usr/bin/env python3
import os
import sys
import subprocess
import urllib.request
import threading
import time
from concurrent.futures import ThreadPoolExecutor

APK_FILE = "sample-render-validation-debug.apk"
TEST_ZIP_FILE = "pixel6pro_base_test.zip"
PACKAGE_NAME = "com.google.android.filament.validation"
ACTIVITY_NAME = ".MainActivity"
RESULTS_DIR = "results"
MAX_WAIT_SECONDS = 600
RETRIES = 3

print_lock = threading.Lock()

def log(device_id, msg):
    """Thread-safe logging utility."""
    with print_lock:
        prefix = f"[{device_id}]" if device_id else "[SYSTEM]"
        print(f"{prefix} {msg}", flush=True)

def run_cmd(cmd, input_data=None, check=True):
    """Run a shell command and return the CompletedProcess."""
    try:
        return subprocess.run(cmd, shell=True, input=input_data, capture_output=True, check=check)
    except subprocess.CalledProcessError as e:
        if check:
            raise
        return e

def check_adb():
    """Verify adb is available."""
    try:
        subprocess.run(["adb", "version"], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        log(None, "Error: adb is not available on the system. Please install adb and ensure it's in your PATH.")
        sys.exit(1)

def download_file(url, filename):
    """Download a file if it doesn't already exist."""
    if os.path.exists(filename):
        log(None, f"{filename} already exists in the current directory, skipping download.")
        return
    log(None, f"Downloading {filename} from {url}...")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(filename, 'wb') as out_file:
            data = response.read()
            out_file.write(data)
        log(None, f"Downloaded {filename}.")
    except Exception as e:
        log(None, f"Failed to download {filename}: {e}")
        sys.exit(1)

def get_connected_devices():
    """Return a set of currently connected device IDs."""
    result = run_cmd("adb devices -l", check=False)
    if result.returncode != 0:
        return set()

    devices = []
    for line in result.stdout.decode('utf-8').splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == 'device':
            devices.append(parts[0])
    return set(devices)

def push_file_to_app_dir(device_id, local_file, remote_file):
    """Push a local file to the app's internal files directory."""
    with open(local_file, "rb") as f:
        data = f.read()
    # Ensure the files directory exists (pm clear might have removed it)
    run_cmd(f"adb -s {device_id} shell \"run-as {PACKAGE_NAME} mkdir -p files\"", check=False)
    cmd = f"adb -s {device_id} shell \"run-as {PACKAGE_NAME} sh -c 'cat > files/{remote_file}'\""
    res = run_cmd(cmd, input_data=data, check=False)
    if res.returncode != 0:
        raise Exception(f"Failed to push {local_file} to {remote_file}: {res.stderr.decode('utf-8')}")

def get_app_files(device_id):
    """List files in the app's internal files directory."""
    cmd = f"adb -s {device_id} shell \"run-as {PACKAGE_NAME} ls files/\""
    res = run_cmd(cmd, check=False)
    if res.returncode == 0:
        return [f.strip() for f in res.stdout.decode('utf-8').splitlines() if f.strip()]
    return []

def pull_file_from_app_dir(device_id, remote_file, local_file):
    """Pull a file from the app's internal files directory."""
    cmd = f"adb -s {device_id} shell \"run-as {PACKAGE_NAME} cat files/{remote_file}\""
    res = run_cmd(cmd, check=False)
    if res.returncode == 0:
        with open(local_file, "wb") as f:
            f.write(res.stdout)
        return True
    return False

def is_app_foreground(device_id):
    """Check if the app is currently in the foreground."""
    cmd = f"adb -s {device_id} shell dumpsys activity activities"
    res = run_cmd(cmd, check=False)
    if res.returncode == 0:
        for line in res.stdout.decode('utf-8', errors='ignore').splitlines():
            if "Resumed" in line and PACKAGE_NAME in line:
                return True
    return False

def take_bugreport(device_id):
    """Take a bugreport and save it to the results directory."""
    bugreport_path = os.path.join(RESULTS_DIR, f"bugreport-{device_id}.zip")
    log(device_id, f"Running adb bugreport {bugreport_path}...")
    run_cmd(f"adb -s {device_id} bugreport {bugreport_path}", check=False)
    log(device_id, f"Bugreport saved to {bugreport_path}.")

def action_a(device_id):
    """Execute Action A for a given device."""
    result_zip_path = os.path.join(RESULTS_DIR, f"{device_id}.zip")
    bugreport_path = os.path.join(RESULTS_DIR, f"bugreport-{device_id}.zip")

    if os.path.exists(result_zip_path):
        log(device_id, f"Result zip {result_zip_path} already exists. Skipping test.")
        return

    if os.path.exists(bugreport_path):
        log(device_id, f"Bugreport {bugreport_path} already exists. Skipping test.")
        return

    log(device_id, "Starting test sequence.")

    for attempt in range(1, RETRIES + 1):
        log(device_id, f"--- Attempt {attempt}/{RETRIES} ---")

        # Install APK
        log(device_id, f"Installing {APK_FILE}...")
        res = run_cmd(f"adb -s {device_id} install -r {APK_FILE}", check=False)
        if res.returncode != 0:
            log(device_id, f"Failed to install APK: {res.stderr.decode('utf-8')}")
            continue

        # Wipe user data to ensure a fresh start
        log(device_id, f"Wiping app data for {PACKAGE_NAME}...")
        run_cmd(f"adb -s {device_id} shell pm clear {PACKAGE_NAME}", check=False)

        initial_files = set(get_app_files(device_id))

        # Push test zip
        log(device_id, f"Pushing test data {TEST_ZIP_FILE}...")
        try:
            push_file_to_app_dir(device_id, TEST_ZIP_FILE, TEST_ZIP_FILE)
        except Exception as e:
            log(device_id, str(e))
            continue

        # Start test
        log(device_id, "Starting test activity...")
        start_cmd = f"adb -s {device_id} shell am start -S -n {PACKAGE_NAME}/{ACTIVITY_NAME} --es zip_path \"{TEST_ZIP_FILE}\" --ez auto_run true"
        res = run_cmd(start_cmd, check=False)
        if res.returncode != 0:
            log(device_id, f"Failed to start activity: {res.stderr.decode('utf-8')}")
            continue

        log(device_id, "Monitoring test progress (max 600s)...")
        start_time = time.time()
        test_success = False
        result_remote_file = None
        seen_foreground = False

        while time.time() - start_time < MAX_WAIT_SECONDS:
            time.sleep(5)

            # Check for new zip files
            current_files = set(get_app_files(device_id))
            new_zips = [f for f in (current_files - initial_files) if f.endswith('.zip') and f != TEST_ZIP_FILE]

            if new_zips:
                result_remote_file = new_zips[0]
                test_success = True
                log(device_id, f"Found result zip on device: {result_remote_file}")
                break

            # Check if still in foreground
            if is_app_foreground(device_id):
                seen_foreground = True
            else:
                if not seen_foreground:
                    log(device_id, "WARNING: App is not in foreground. Please ensure the device screen is turned on and unlocked.")
                else:
                    # Wait briefly to ensure files are written, then check one last time
                    time.sleep(2)
                    current_files = set(get_app_files(device_id))
                    new_zips = [f for f in (current_files - initial_files) if f.endswith('.zip') and f != TEST_ZIP_FILE]
                    if new_zips:
                        result_remote_file = new_zips[0]
                        test_success = True
                        log(device_id, f"Found result zip just after app exited: {result_remote_file}")
                        break

                    log(device_id, "App is no longer in foreground and no result zip was found.")
                    break

        if test_success and result_remote_file:
            log(device_id, f"Pulling result zip...")
            if pull_file_from_app_dir(device_id, result_remote_file, result_zip_path):
                log(device_id, f"SUCCESS! Result saved to {result_zip_path}")
                return # We are done with this device
            else:
                log(device_id, "Failed to pull result zip.")
        else:
            if time.time() - start_time >= MAX_WAIT_SECONDS:
                log(device_id, "Test timed out.")

    # If we fall through the retries loop, the test failed
    log(device_id, "Test failed after all retries.")
    take_bugreport(device_id)

def main():
    check_adb()

    version = os.environ.get("TEST_VERSION", "041626")
    base_url = f"https://raw.githubusercontent.com/google/filament-assets/main/cts/{version}"

    download_file(f"{base_url}/{APK_FILE}", APK_FILE)
    download_file(f"{base_url}/{TEST_ZIP_FILE}", TEST_ZIP_FILE)

    if not os.path.exists(RESULTS_DIR):
        os.makedirs(RESULTS_DIR)
        log(None, f"Created '{RESULTS_DIR}' directory.")

    completed_devices = set()
    active_devices = set()

    # Using ThreadPoolExecutor to run tests on multiple devices in parallel
    executor = ThreadPoolExecutor(max_workers=10)
    futures = {}

    log(None, "Monitoring for connected devices. Press Ctrl+C to quit.")
    prompt_shown = False

    try:
        while True:
            current_devices = get_connected_devices()
            new_devices = current_devices - completed_devices - active_devices

            for device_id in new_devices:
                log(device_id, "New device detected. Scheduling test.")
                active_devices.add(device_id)
                prompt_shown = False
                future = executor.submit(action_a, device_id)
                futures[future] = device_id

            # Clean up finished tasks
            done_futures = []
            for future, device_id in list(futures.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception as e:
                        log(device_id, f"Exception in worker thread: {e}")
                    active_devices.remove(device_id)
                    completed_devices.add(device_id)
                    done_futures.append(future)

            for future in done_futures:
                del futures[future]

            if not active_devices and completed_devices and not prompt_shown:
                log(None, "All running tests have finished. Plug in a new device or press Ctrl+C to quit.")
                prompt_shown = True

            time.sleep(3)
    except KeyboardInterrupt:
        log(None, "Ctrl+C detected. Shutting down...")
        executor.shutdown(wait=False)
        sys.exit(0)

if __name__ == "__main__":
    main()
    sys.exit(0)
