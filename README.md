# Temtop LKC-1000S+ 2nd Gen Air Quality Monitor Serial/Modbus Interface

A reverse-engineering and monitoring project to extract and log real-time air quality sensor readings (PM2.5, PM10, HCHO, TVOC, Temperature, Humidity, and Particle count) from the **Temtop LKC-1000S+ 2nd Gen** handheld monitor. 

The device exposes a standard Modbus RTU interface over its USB Virtual COM port (typically `/dev/ttyACM0` or `/dev/ttyACM1` on Linux).

---

## Technical Discoveries

Through protocol analysis, we discovered that:
* **Protocol:** Modbus RTU
* **Default Slave ID:** `0xFE` (254)
* **Default Baud Rate:** `115200`
* **Function Code:** `04` (Read Input Registers)
* **Firmware Bug (Critical):** Querying registers one-by-one at addresses `0–2` or `17+` triggers a microcontroller crash/reset due to a bounds-checking bug in the device's custom Modbus stack.
* **Bulk Read Bypass:** Querying registers `0–32` (quantity `33`) in a single contiguous Modbus bulk read transaction works perfectly and bypasses the crash condition, allowing stable data streaming.

---

## Verified Register Map (Function Code 04)

| Register | Parameter | Data Type | Scale Factor | Example (Raw $\rightarrow$ Interpreted) |
| :--- | :--- | :--- | :--- | :--- |
| **Reg 0** | RTC Clock: Year | Unsigned 16-bit | Raw integer | `2026` |
| **Reg 1** | RTC Clock: Month | Unsigned 16-bit | Raw integer | `6` |
| **Reg 2** | RTC Clock: Day | Unsigned 16-bit | Raw integer | `18` |
| **Reg 3** | RTC Clock: Hour | Unsigned 16-bit | Raw integer | `7` |
| **Reg 4** | RTC Clock: Minute | Unsigned 16-bit | Raw integer | `19` |
| **Reg 5** | RTC Clock: Second | Unsigned 16-bit | Raw integer | `58` |
| **Reg 6** | PM2.5 concentration | Unsigned 16-bit | `value / 10` | `11` $\rightarrow$ `1.1 µg/m³` |
| **Reg 7** | PM10 concentration | Unsigned 16-bit | `value / 10` | `19` $\rightarrow$ `1.9 µg/m³` |
| **Reg 8** | Reserved / Pinned | Unsigned 16-bit | - | `0` |
| **Reg 9** | HCHO (Formaldehyde) | Unsigned 16-bit | `value / 1000` | `30` $\rightarrow$ `0.030 mg/m³` |
| **Reg 10** | TVOC | Unsigned 16-bit | `value / 1000` | `130` $\rightarrow$ `0.130 mg/m³` |
| **Reg 11–15** | Reserved / Pinned | Unsigned 16-bit | - | `0` |
| **Reg 16** | Temperature | Signed 16-bit | `value / 100` | `7780` $\rightarrow$ `77.80 °F` (or °C) |
| **Reg 17** | Humidity | Unsigned 16-bit | `value / 100` | `4320` $\rightarrow$ `43.20 %RH` |
| **Reg 18** | Reserved / Pinned | Unsigned 16-bit | - | `0` |
| **Reg 19** | Particle count | Unsigned 16-bit | Raw integer | `191` $\rightarrow$ `191 per/L` |
| **Reg 20–32** | Model ID, duplicate RTC, constants | Unsigned 16-bit | - | Diagnostics/RTC data |

*Note: Air Quality Index (AQI) is not transmitted as a register because it is computed on the fly by the monitoring script using US EPA concentration breakpoints.*

---

## Getting Started

### 1. Installation
Install the serial port dependencies:
```bash
pip install pyserial
```

### 2. Connect the Device
Plug the Temtop monitor into your computer's USB port. It should register as a Virtual COM port (e.g. `/dev/ttyACM0` or `/dev/ttyACM1` on Linux).

Ensure your user has access permissions:
```bash
sudo usermod -a -G dialout $USER
# (Log out and log back in to apply group changes)
```

### 3. Run the Live Monitor & Logger
To start real-time data capture, display the live dashboard, and log values to a CSV file:
```bash
python3 temtop_monitor.py --port /dev/ttyACM1 --interval 5.0 --output my_air_readings.csv
```

---

## Utility Scripts

*   `fuzzer.py`: General port fuzzer used to test default baud rates and discover initial responsive command headers.
*   `modbus_scanner.py`: Scanner used to sweep registers using FC 03 and FC 04 to identify active ranges and translate exceptions.
*   `modbus_logger.py`: Configurable raw register logger used to collect raw register logs to cross-reference with the device screen.
