#!/usr/bin/env python3
"""
Modbus RTU CSV Data Logger for Temtop LKC-1000S+ 2nd Gen.
Polls registers 3-16, prints a live dashboard, and logs the raw values to a CSV.
"""

import argparse
import csv
import datetime
import os
import sys
import time
from typing import List, Optional, Tuple
import serial
import serial.tools.list_ports

# Colors for terminal styling
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
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

def wait_and_reopen(port: str, baud: int, timeout: float) -> serial.Serial:
    """Blocks and repeatedly attempts to reopen the serial port after a connection drop."""
    print(f"\n{color('[!]', Colors.WARNING)} Port disconnected. Waiting for {port} to reappear...")
    while True:
        try:
            time.sleep(1.0)
            ser = serial.Serial(port, baud, timeout=timeout)
            print(f"\n{color('[*]', Colors.GREEN)} Reconnected successfully!")
            time.sleep(1.5)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except (serial.SerialException, OSError, FileNotFoundError, PermissionError):
            sys.stdout.write(".")
            sys.stdout.flush()

def read_registers_bulk(ser: serial.Serial, slave_id: int, start_reg: int, quantity: int, timeout: float) -> Tuple[str, Optional[List[int]]]:
    """Attempts to read multiple registers in a single bulk Modbus command."""
    req = make_modbus_request(slave_id, func_code=4, register=start_reg, quantity=quantity)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(req)
        ser.flush()
    except (serial.SerialException, OSError) as e:
        return f"WRITE_ERROR: {e}", None
        
    expected_len = 5 + 2 * quantity
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
        for i in range(quantity):
            val = int.from_bytes(data[i*2:(i+1)*2], byteorder='big', signed=False)
            values.append(val)
        return "SUCCESS", values
    return status, None

def read_registers_one_by_one(ser: serial.Serial, slave_id: int, start_reg: int, quantity: int, timeout: float) -> Tuple[str, Optional[List[int]]]:
    """Fallback method: queries registers individually and aggregates the results."""
    values = []
    for reg in range(start_reg, start_reg + quantity):
        req = make_modbus_request(slave_id, func_code=4, register=reg, quantity=1)
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            ser.write(req)
            ser.flush()
        except (serial.SerialException, OSError) as e:
            return f"WRITE_ERROR_REG_{reg}: {e}", None
            
        time.sleep(0.04)
        response = b""
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            if ser.in_waiting > 0:
                try:
                    response += ser.read(ser.in_waiting)
                except (serial.SerialException, OSError):
                    return f"READ_ERROR_REG_{reg}", None
            if len(response) >= 7 or (len(response) >= 5 and (response[1] & 0x80)):
                break
            time.sleep(0.01)
            
        status, data = parse_modbus_response(response, slave_id, expected_func=4)
        if status == "SUCCESS" and data is not None:
            val = int.from_bytes(data, byteorder='big', signed=False)
            values.append(val)
        else:
            return f"FAILED_REG_{reg}: {status}", None
            
        time.sleep(0.02)
    return "SUCCESS", values

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor and log Temtop registers to a CSV file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-p", "--port", default="/dev/ttyACM0", help="Serial/USB port device path")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate")
    parser.add_argument("-s", "--slave", default="0xFE", help="Modbus Slave ID")
    parser.add_argument("-r", "--range", default="0-32", help="Range of registers to log (e.g. 0-32 or 3-16)")
    parser.add_argument("-i", "--interval", type=float, default=5.0, help="Polling interval in seconds")
    parser.add_argument("-o", "--output", default="temtop_log.csv", help="Output CSV filename")
    parser.add_argument("-t", "--timeout", type=float, default=0.5, help="Response timeout in seconds")
    parser.add_argument("--force-individual", action="store_true", help="Force querying registers individually instead of bulk")
    
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
        
    # Parse register range
    try:
        if "-" in args.range:
            start_str, end_str = args.range.split("-")
            start_reg = int(start_str)
            end_reg = int(end_str)
        else:
            start_reg = int(args.range)
            end_reg = start_reg
    except ValueError:
        print(f"Invalid register range: {args.range}")
        sys.exit(1)
        
    quantity = end_reg - start_reg + 1
    
    # Write CSV header if file does not exist
    file_exists = os.path.exists(args.output)
    try:
        with open(args.output, 'a', newline='') as csvfile:
            writer = csv.writer(csvfile)
            if not file_exists:
                header = ["Timestamp"] + [f"Reg_{r}" for r in range(start_reg, start_reg + quantity)]
                writer.writerow(header)
                print(f"Created new CSV file: {args.output}")
    except OSError as e:
        print(f"Error opening/writing CSV file {args.output}: {e}")
        sys.exit(1)
        
    print(color("=== Temtop Modbus RTU Data Logger ===", Colors.HEADER + Colors.BOLD))
    print(f"Logging to:    {color(args.output, Colors.CYAN)}")
    print(f"Port:          {color(args.port, Colors.CYAN)}")
    print(f"Baud:          {color(str(args.baud), Colors.CYAN)}")
    print(f"Interval:      {color(f'{args.interval}s', Colors.CYAN)}")
    print("Press Ctrl+C to abort.\n")
    
    try:
        ser = serial.Serial(args.port, args.baud, timeout=args.timeout)
    except (serial.SerialException, OSError) as e:
        print(f"{color('[!]', Colors.FAIL)} Error opening port {args.port}: {e}")
        sys.exit(1)
        
    use_bulk = not args.force_individual
    
    try:
        while True:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Retrieve data
            try:
                if use_bulk:
                    status, values = read_registers_bulk(ser, slave_id, start_reg, quantity, args.timeout)
                    if status != "SUCCESS":
                        # If bulk read fails, try one-by-one fallback
                        status, values = read_registers_one_by_one(ser, slave_id, start_reg, quantity, args.timeout)
                        if status == "SUCCESS":
                            # Bulk read is unsupported, stick to individual
                            use_bulk = False
                else:
                    status, values = read_registers_one_by_one(ser, slave_id, start_reg, quantity, args.timeout)
            except (serial.SerialException, OSError) as e:
                # Connection lost during transaction
                print(f"\n{color('[!]', Colors.FAIL)} Serial interface error: {e}")
                ser = wait_and_reopen(args.port, args.baud, args.timeout)
                continue
                
            if status == "SUCCESS" and values is not None:
                # Log to CSV
                try:
                    with open(args.output, 'a', newline='') as csvfile:
                        writer = csv.writer(csvfile)
                        writer.writerow([timestamp] + values)
                except OSError as e:
                    print(f"\n{color('[!]', Colors.FAIL)} Error writing to CSV file: {e}")
                    
                # Update console dashboard
                # Use clear terminal codes or simple reprint
                sys.stdout.write("\033[H\033[J") # clear screen
                print(color("=== Temtop Live Dashboard ===", Colors.HEADER + Colors.BOLD))
                print(f"File: {args.output} | Port: {args.port} | Mode: {'Bulk' if use_bulk else 'Individual'}")
                print(f"Last Logged: {timestamp}\n")
                
                # Format into a clean, unclipped table with common scale factors
                print("-" * 75)
                print(f"{'Register':<9} | {'Raw Dec':<8} | {'Raw Hex':<7} | {'val/10':<7} | {'val/100':<7} | {'val/1000'}")
                print("-" * 75)
                for reg_idx, val in enumerate(values):
                    reg_addr = start_reg + reg_idx
                    # Convert to signed 16-bit to show negative temperatures if any
                    val_signed = val if val < 32768 else val - 65536
                    div_10 = f"{val_signed / 10.0:.1f}"
                    div_100 = f"{val_signed / 100.0:.2f}"
                    div_1000 = f"{val_signed / 1000.0:.3f}"
                    print(f" Reg {reg_addr:<2d}   | {val:<8d} | 0x{val:04X}   | {div_10:<7} | {div_100:<7} | {div_1000}")
                print("-" * 75)
                
            else:
                print(f"[{timestamp}] {color('Polling failed', Colors.FAIL)}: {status}")
                
            time.sleep(args.interval)
            
    except KeyboardInterrupt:
        print(f"\n\n{color('[*]', Colors.WARNING)} Logging stopped by user.")
    finally:
        if ser.is_open:
            ser.close()
            print(f"{color('[*]', Colors.BLUE)} Serial port closed.")

if __name__ == "__main__":
    main()
