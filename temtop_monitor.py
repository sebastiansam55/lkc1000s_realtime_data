#!/usr/bin/env python3
"""
Temtop LKC-1000S+ 2nd Gen Real-Time Air Quality Monitor & Logger.
Polls Modbus registers 0-32 in a single bulk transaction, decodes them
according to the verified register map, calculates US EPA AQI, and logs to CSV.
"""

import argparse
import csv
import datetime
import os
import sys
import time
from typing import Dict, List, Optional, Tuple
import serial
import serial.tools.list_ports

# Colors for terminal styling
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'  # Yellow
    ORANGE = '\033[38;5;208m'
    FAIL = '\033[91m'      # Red
    PURPLE = '\033[35m'
    END = '\033[0m'
    BOLD = '\033[1m'

USE_COLOR = sys.stdout.isatty()

def color(text: str, color_code: str) -> str:
    """Return colored text if stdout is a TTY/terminal, else return plain text."""
    return f"{color_code}{text}{Colors.END}" if USE_COLOR else text

def calculate_crc(data: bytes) -> bytes:
    """Calculate the Modbus RTU CRC-16 checksum (low-byte first)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])

def make_modbus_request(slave_id: int, func_code: int, register: int, quantity: int) -> bytes:
    """Construct a standard Modbus RTU request frame."""
    frame = bytes([
        slave_id,
        func_code,
        (register >> 8) & 0xFF,
        register & 0xFF,
        (quantity >> 8) & 0xFF,
        quantity & 0xFF
    ])
    return frame + calculate_crc(frame)

def parse_modbus_response(response: bytes, expected_slave: int, expected_func: int) -> Tuple[str, Optional[bytes]]:
    """Parses a Modbus RTU response frame. Returns a tuple of (status_string, data_bytes)."""
    if len(response) < 5:
        return "TIMEOUT/SHORT_RESPONSE", None
    
    slave_id = response[0]
    func_code = response[1]
    
    if slave_id != expected_slave:
        return f"SLAVE_MISMATCH (Expected {expected_slave}, got {slave_id})", None
    
    # Check for Modbus Exception
    if func_code == (expected_func | 0x80):
        exception_code = response[2]
        exc_names = {
            1: "Illegal Function (0x01)",
            2: "Illegal Data Address (0x02)",
            3: "Illegal Data Value (0x03)",
            4: "Slave Device Failure (0x04)"
        }
        return f"EXCEPTION: {exc_names.get(exception_code, f'Code {exception_code}')}", None
        
    if func_code != expected_func:
        return f"FUNC_MISMATCH (Expected {expected_func}, got {func_code})", None
        
    byte_count = response[2]
    if len(response) < 3 + byte_count + 2:
        return "INCOMPLETE_FRAME", None
        
    data = response[3:3+byte_count]
    crc_recv = response[3+byte_count:3+byte_count+2]
    crc_calc = calculate_crc(response[:3+byte_count])
    
    if crc_recv != crc_calc:
        return "CRC_ERROR", None
        
    return "SUCCESS", data

def calculate_aqi_pm25(c: float) -> int:
    """Calculates the US EPA Air Quality Index for PM2.5 concentration (ug/m3)."""
    c = round(c, 1)
    if 0.0 <= c <= 12.0:
        return int(round((50 - 0) / (12.0 - 0.0) * (c - 0.0) + 0))
    elif 12.1 <= c <= 35.4:
        return int(round((100 - 51) / (35.4 - 12.1) * (c - 12.1) + 51))
    elif 35.5 <= c <= 55.4:
        return int(round((150 - 101) / (55.4 - 35.5) * (c - 35.5) + 101))
    elif 55.5 <= c <= 150.4:
        return int(round((200 - 151) / (150.4 - 55.5) * (c - 55.5) + 151))
    elif 150.5 <= c <= 250.4:
        return int(round((300 - 201) / (250.4 - 150.5) * (c - 150.5) + 201))
    elif 250.5 <= c <= 350.4:
        return int(round((400 - 301) / (350.4 - 250.5) * (c - 250.5) + 301))
    elif 350.5 <= c <= 500.4:
        return int(round((500 - 401) / (500.4 - 350.5) * (c - 350.5) + 401))
    return 500

def calculate_aqi_pm10(c: float) -> int:
    """Calculates the US EPA Air Quality Index for PM10 concentration (ug/m3)."""
    c = int(round(c))
    if 0 <= c <= 54:
        return int(round((50 - 0) / (54 - 0) * (c - 0) + 0))
    elif 55 <= c <= 154:
        return int(round((100 - 51) / (154 - 55) * (c - 55) + 51))
    elif 155 <= c <= 254:
        return int(round((150 - 101) / (254 - 155) * (c - 155) + 101))
    elif 255 <= c <= 354:
        return int(round((200 - 151) / (354 - 255) * (c - 255) + 151))
    elif 355 <= c <= 424:
        return int(round((300 - 201) / (424 - 355) * (c - 355) + 201))
    elif 425 <= c <= 504:
        return int(round((400 - 301) / (504 - 425) * (c - 425) + 301))
    elif 505 <= c <= 604:
        return int(round((500 - 401) / (604 - 505) * (c - 505) + 401))
    return 500

def get_aqi_color_and_text(aqi: int) -> Tuple[str, str]:
    """Returns the ANSI color code and description for a given AQI value."""
    if aqi <= 50:
        return Colors.GREEN, "Good"
    elif aqi <= 100:
        return Colors.WARNING, "Moderate"
    elif aqi <= 150:
        return Colors.ORANGE, "Unhealthy for Sensitive Groups"
    elif aqi <= 200:
        return Colors.FAIL, "Unhealthy"
    elif aqi <= 300:
        return Colors.PURPLE, "Very Unhealthy"
    else:
        return Colors.FAIL + Colors.BOLD, "Hazardous"

def read_modbus_data(ser: serial.Serial, slave_id: int, timeout: float) -> Tuple[str, Optional[List[int]]]:
    """Queries registers 0-32 in a single bulk transaction (FC 04)."""
    # 33 registers = 66 bytes of data
    # Expected frame size: slave(1) + func(1) + bytes(1) + data(66) + crc(2) = 71 bytes
    req = make_modbus_request(slave_id, func_code=4, register=0, quantity=33)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(req)
        ser.flush()
    except (serial.SerialException, OSError) as e:
        return f"WRITE_ERROR: {e}", None
        
    expected_len = 71
    time.sleep(0.08)
    
    response = b""
    start_time = time.time()
    while (time.time() - start_time) < timeout:
        if ser.in_waiting > 0:
            try:
                response += ser.read(ser.in_waiting)
            except (serial.SerialException, OSError) as e:
                return f"READ_ERROR: {e}", None
        if len(response) >= expected_len or (len(response) >= 5 and (response[1] & 0x80)):
            break
        time.sleep(0.01)
        
    status, data = parse_modbus_response(response, slave_id, expected_func=4)
    if status == "SUCCESS" and data is not None:
        values = []
        for i in range(33):
            val = int.from_bytes(data[i*2:(i+1)*2], byteorder='big', signed=False)
            values.append(val)
        return "SUCCESS", values
    return status, None

def wait_and_reopen(port: str, baud: int, timeout: float) -> serial.Serial:
    """Blocks and retries opening the serial interface until successful."""
    print(f"\n{color('[!]', Colors.WARNING)} Port disconnected. Attempting reconnection...")
    while True:
        try:
            time.sleep(1.5)
            ser = serial.Serial(port, baud, timeout=timeout)
            print(f"\n{color('[*]', Colors.GREEN)} Reconnected to {port} successfully!")
            time.sleep(1.0)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except (serial.SerialException, OSError, FileNotFoundError, PermissionError):
            sys.stdout.write(".")
            sys.stdout.flush()

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Temtop LKC-1000S+ 2nd Gen Real-Time Monitor and Logger.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-p", "--port", default="/dev/ttyACM0", help="Serial/USB port device path")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("-s", "--slave", default="0xFE", help="Modbus Slave ID")
    parser.add_argument("-i", "--interval", type=float, default=5.0, help="Polling interval in seconds")
    parser.add_argument("-o", "--output", default="temtop_readings.csv", help="Output CSV filename")
    parser.add_argument("-t", "--timeout", type=float, default=0.5, help="Response timeout in seconds")
    
    args = parser.parse_args()
    
    # Parse slave ID
    try:
        if args.slave.lower().startswith("0x"):
            slave_id = int(args.slave, 16)
        else:
            slave_id = int(args.slave)
    except ValueError:
        print(f"Invalid slave ID: {args.slave}")
        sys.exit(1)
        
    # Write CSV header if file doesn't exist
    file_exists = os.path.exists(args.output)
    try:
        with open(args.output, 'a', newline='') as csvfile:
            writer = csv.writer(csvfile)
            if not file_exists:
                writer.writerow([
                    "Timestamp", "Device_Time", 
                    "PM2.5(ug/m3)", "PM10(ug/m3)", "Particles(per/L)", 
                    "AQI_PM2.5", "AQI_PM10", "AQI_Overall",
                    "HCHO(mg/m3)", "TVOC(mg/m3)", 
                    "Temp(F)", "Temp(C)", "Humidity(%RH)"
                ])
                print(f"Created new CSV file: {args.output}")
    except OSError as e:
        print(f"Error initializing CSV file: {e}")
        sys.exit(1)
        
    try:
        ser = serial.Serial(args.port, args.baud, timeout=args.timeout)
    except (serial.SerialException, OSError) as e:
        print(f"{color('[!]', Colors.FAIL)} Error opening port {args.port}: {e}")
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if ports:
            print(f"Available ports: {', '.join(ports)}")
        sys.exit(1)
        
    print(f"{color('[*]', Colors.GREEN)} Successfully connected to {args.port}.")
    print("Initializing real-time display. Press Ctrl+C to stop.")
    time.sleep(1.0)
    
    try:
        while True:
            # Poll data
            try:
                status, regs = read_modbus_data(ser, slave_id, args.timeout)
            except (serial.SerialException, OSError) as e:
                print(f"\n{color('[!]', Colors.FAIL)} Communication error: {e}")
                ser = wait_and_reopen(args.port, args.baud, args.timeout)
                continue
                
            pc_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            if status == "SUCCESS" and regs is not None:
                # 1. Parse RTC
                dev_year = regs[0]
                dev_month = regs[1]
                dev_day = regs[2]
                dev_hour = regs[3]
                dev_minute = regs[4]
                dev_second = regs[5]
                dev_time_str = f"{dev_year:04d}-{dev_month:02d}-{dev_day:02d} {dev_hour:02d}:{dev_minute:02d}:{dev_second:02d}"
                
                # 2. Parse sensor data
                pm25 = regs[6] / 10.0
                pm10 = regs[7] / 10.0
                hcho = regs[9] / 1000.0
                tvoc = regs[10] / 1000.0
                
                # Temp conversion
                raw_temp = regs[16]
                # Temperature is stored as signed 16-bit
                temp_signed = raw_temp if raw_temp < 32768 else raw_temp - 65536
                temp_val = temp_signed / 100.0
                # Note: Temp is represented in whatever unit is active on device
                # Usually F or C. We check size to estimate.
                # If temp_val > 50, it is likely Fahrenheit.
                if temp_val > 50.0:
                    temp_f = temp_val
                    temp_c = (temp_f - 32.0) * 5.0 / 9.0
                else:
                    temp_c = temp_val
                    temp_f = (temp_c * 9.0 / 5.0) + 32.0
                    
                humidity = regs[17] / 100.0
                particles = regs[19]
                
                # 3. Calculate AQI
                aqi_pm25 = calculate_aqi_pm25(pm25)
                aqi_pm10 = calculate_aqi_pm10(pm10)
                aqi_overall = max(aqi_pm25, aqi_pm10)
                
                # 4. Log to CSV
                try:
                    with open(args.output, 'a', newline='') as csvfile:
                        writer = csv.writer(csvfile)
                        writer.writerow([
                            pc_timestamp, dev_time_str,
                            f"{pm25:.1f}", f"{pm10:.1f}", particles,
                            aqi_pm25, aqi_pm10, aqi_overall,
                            f"{hcho:.3f}", f"{tvoc:.3f}",
                            f"{temp_f:.2f}", f"{temp_c:.2f}", f"{humidity:.2f}"
                        ])
                except OSError as e:
                    print(f"\n{color('[!]', Colors.FAIL)} Error writing to CSV file: {e}")
                    
                # 5. Render live dashboard
                sys.stdout.write("\033[H\033[J") # Clear console screen
                print(color("=== TEMTOP LKC-1000S+ REAL-TIME MONITOR ===", Colors.HEADER + Colors.BOLD))
                print(f"Log File:   {color(args.output, Colors.CYAN)}")
                print(f"Port:       {color(args.port, Colors.CYAN)} | Baud: {args.baud}")
                print(f"PC Time:    {pc_timestamp}")
                print(f"Device RTC: {dev_time_str}")
                print("=" * 48)
                
                # AQI Block
                aqi_col, aqi_text = get_aqi_color_and_text(aqi_overall)
                print(f" Overall AQI:    {color(f' {aqi_overall:<3d} ', aqi_col + Colors.BOLD)}  ({color(aqi_text, aqi_col)})")
                print("-" * 48)
                
                # PM & Particle Block
                print(f" PM2.5 Concentration : {color(f'{pm25:>5.1f}', Colors.BOLD)} µg/m³  (AQI: {aqi_pm25})")
                print(f" PM10 Concentration  : {color(f'{pm10:>5.1f}', Colors.BOLD)} µg/m³  (AQI: {aqi_pm10})")
                print(f" Particle Count      : {color(f'{particles:>5d}', Colors.BOLD)} per/L")
                print("-" * 48)
                
                # HCHO & TVOC Block
                # Safety thresholds: HCHO <= 0.08 mg/m3, TVOC <= 0.5 mg/m3
                hcho_col = Colors.GREEN if hcho <= 0.08 else Colors.FAIL
                tvoc_col = Colors.GREEN if tvoc <= 0.5 else (Colors.WARNING if tvoc <= 1.0 else Colors.FAIL)
                
                print(f" HCHO (Formaldehyde) : {color(f'{hcho:>5.3f}', hcho_col + Colors.BOLD)} mg/m³ " + 
                      (color("[SAFE]", Colors.GREEN) if hcho <= 0.08 else color("[HIGH]", Colors.FAIL)))
                print(f" TVOC                : {color(f'{tvoc:>5.3f}', tvoc_col + Colors.BOLD)} mg/m³ " + 
                      (color("[GOOD]", Colors.GREEN) if tvoc <= 0.5 else (color("[MOD]", Colors.WARNING) if tvoc <= 1.0 else color("[POOR]", Colors.FAIL))))
                print("-" * 48)
                
                # Environmental Block
                print(f" Temperature         : {color(f'{temp_f:>5.1f}', Colors.BOLD)} °F  /  {color(f'{temp_c:>4.1f}', Colors.BOLD)} °C")
                print(f" Humidity            : {color(f'{humidity:>5.1f}', Colors.BOLD)} %RH")
                print("=" * 48)
                print(color("[*] Polling sensor. Press Ctrl+C to exit.", Colors.BLUE))
                
            else:
                print(f"[{pc_timestamp}] {color('Failed to poll device', Colors.FAIL)}: {status}")
                
            time.sleep(args.interval)
            
    except KeyboardInterrupt:
        print(f"\n\n{color('[*]', Colors.WARNING)} Monitor stopped by user.")
    finally:
        if ser.is_open:
            ser.close()
            print(f"{color('[*]', Colors.BLUE)} Serial connection closed.")

if __name__ == "__main__":
    main()
