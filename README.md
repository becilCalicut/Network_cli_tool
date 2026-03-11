# BKP Application – Network Backup Tool

A multi-threaded Python/Tkinter GUI for capturing command output from
Cisco, Aruba, Ruckus, and Alcatel switches via SSH.

---

## Requirements

Python 3.10+ and the `paramiko` SSH library.

```bash
pip install -r requirements.txt
```

---

## How to Run

```bash
python bkp_application.py
```

---

## Directory Structure

Place these command files **in the same folder where you run the script**:

```
bkp_application.py
requirements.txt
cisco.txt        ← commands for Cisco devices
aruba.txt        ← commands for Aruba devices
ruckus.txt       ← commands for Ruckus devices
alcatel.txt      ← commands for Alcatel devices
```

If a command file is missing when you hit **RUN BACKUP**, the app will:
1. Show a red error in the Status Screen.
2. Auto-create a template `.txt` file you can edit.

---

## Command File Format

Each OEM's `.txt` file contains one CLI command per line.  
Lines beginning with `#` are treated as comments.

Example – `cisco.txt`:
```
# Cisco IOS commands
show version
show running-config
show interfaces status
show ip route
```

---

## Switch IP File

A plain `.txt` file with one IP per line:
```
192.168.1.1
192.168.1.2
10.0.0.50
```
Lines starting with `#` are ignored.

---

## Backup Output

Each switch produces a `.txt` file named after its IP address
(dots replaced with underscores), e.g. `192_168_1_1.txt`.

---

## Application Fields

| Field                    | Description                                             |
|--------------------------|---------------------------------------------------------|
| Switch OEM               | Select the vendor (Cisco / Aruba / Ruckus / Alcatel)    |
| User Name                | SSH login username for the switches                     |
| Password                 | SSH login password                                      |
| Switch IP Directory      | Browse to a `.txt` file listing switch IPs              |
| Backup Storing Directory | Browse to a folder where output files will be saved     |

---

## Concurrency

The app runs up to **10 SSH sessions simultaneously** (configurable via
`max_threads` in `_start_backup`). For 40-50 switches this typically
completes in under 2 minutes depending on network latency.

---

## Notes

* The app uses `paramiko` with `AutoAddPolicy`, so unknown host keys are
  accepted automatically. Adjust `set_missing_host_key_policy` if you need
  stricter security.
* SSH timeout per command defaults to **30 seconds**.
