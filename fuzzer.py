#!/usr/bin/env python3
"""
USB/Serial Port Fuzzer for Air Quality Monitors (and similar MCU devices).
Discovers activation/query commands by sending structured ASCII/hex payloads
and checking for responses.
"""

import argparse
import sys
import time
from typing import Dict
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

def format_bytes_hex(data: bytes) -> str:
    """Format bytes as space-separated uppercase hexadecimal values (e.g. 'AA B4 04')."""
    return " ".join(f"{b:02X}" for b in data)

def format_bytes_ascii(data: bytes) -> str:
    """Safely format bytes as an ASCII representation, escaping non-printable characters."""
    res = []
    for b in data:
        if 32 <= b <= 126:
            res.append(chr(b))
        elif b == 10:
            res.append(r"\n")
        elif b == 13:
            res.append(r"\r")
        elif b == 9:
            res.append(r"\t")
        else:
            res.append(f"\\x{b:02x}")
    return "".join(res)

def get_payloads() -> Dict[str, bytes]:
    """Generates a structured dictionary of test payloads for the fuzzer."""
    payloads: Dict[str, bytes] = {}
    
    # 1. ASCII commands with line ending variations
    ascii_cmds = ["READ", "GET", "DATA", "STATUS", "?", "HELP"]
    endings = {
        "raw": "",
        "CR": "\r",
        "LF": "\n",
        "CRLF": "\r\n"
    }
    for cmd in ascii_cmds:
        for end_name, end_val in endings.items():
            payloads[f"ASCII_{cmd}_{end_name}"] = (cmd + end_val).encode('ascii')
            
    # 2. Common wake-up hex bytes for Chinese environmental sensors
    hex_bytes = {
        "HEX_WAKE_0xAA": b"\xAA",
        "HEX_WAKE_0x55": b"\x55",
        "HEX_WAKE_0xCC": b"\xCC",
        "HEX_WAKE_0xFF": b"\xFF",
        "HEX_WAKE_0x00": b"\x00"
    }
    payloads.update(hex_bytes)
    
    # 3. Known Air Quality Sensor query/config packets (for reference & testing)
    sensor_packets = {
        "SENSEAIR_S8_CO2_READ": b"\xFE\x04\x00\x03\x00\x01\xD5\xC5",
        "PLANTOWER_PMS_PASSIVE_MODE": b"\x42\x4D\xE1\x00\x00\x01\x70",
        "PLANTOWER_PMS_PASSIVE_READ": b"\x42\x4D\xE2\x00\x00\x01\x71",
        "PLANTOWER_PMS_ACTIVE_MODE": b"\x42\x4D\xE1\x00\x01\x01\x71",
        "SDS011_QUERY_DATA": b"\xAA\xB4\x04\x00\x00\x00\x00\x00\x00\xFF\xFF\x02\xAB",
        "SDS011_SET_PASSIVE": b"\xAA\xB4\x02\x01\x01\x00\x00\x00\x00\xFF\xFF\x01\xAB",
        "MH_Z19_CO2_READ": b"\xFF\x01\x86\x00\x00\x00\x00\x00\x79",
        "WINSEN_ZH03B_PASSIVE_INIT": b"\xFF\x01\x35\x00\x00\x00\x00\x00\xCA"
    }
    payloads.update(sensor_packets)
    
    return payloads

def wait_and_reopen(port: str, baud: int, timeout: float) -> serial.Serial:
    """Blocks and repeatedly attempts to reopen the serial port after a connection drop."""
    print(f"\n{color('[!]', Colors.WARNING)} Port disconnected. Waiting for {port} to reappear...")
    while True:
        try:
            time.sleep(1.0)
            ser = serial.Serial(port, baud, timeout=timeout)
            print(f"\n{color('[*]', Colors.GREEN)} Reconnected to {port} successfully!")
            # Allow the MCU USB stack to settle after re-enumeration
            time.sleep(1.5)
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            return ser
        except (serial.SerialException, OSError, FileNotFoundError, PermissionError):
            sys.stdout.write(".")
            sys.stdout.flush()

def run_fuzzer(port: str, baud: int, timeout: float, delay: float, pause_on_hit: bool) -> None:
    """Runs the fuzzing loop over the constructed payload dictionary."""
    payloads = get_payloads()
    total_payloads = len(payloads)
    
    print(color("=== USB/Serial Port Fuzzer ===", Colors.HEADER + Colors.BOLD))
    print(f"Target Port: {color(port, Colors.CYAN)}")
    print(f"Baud Rate:   {color(str(baud), Colors.CYAN)}")
    print(f"Timeout:     {color(f'{timeout}s', Colors.CYAN)}")
    print(f"Delay:       {color(f'{delay}s', Colors.CYAN)}")
    print(f"Payloads:    {color(str(total_payloads), Colors.CYAN)}")
    print("=" * 30 + "\n")
    
    try:
        ser = serial.Serial(port, baud, timeout=timeout)
        print(f"{color('[*]', Colors.GREEN)} Successfully opened serial port.")
    except (serial.SerialException, OSError) as e:
        print(f"{color('[!]', Colors.FAIL)} Error opening port: {e}")
        # Helpfully display other available COM ports
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if ports:
            print(f"Available serial ports: {', '.join(ports)}")
        sys.exit(1)
        
    hits = 0
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    
    try:
        for idx, (name, payload) in enumerate(payloads.items(), 1):
            # Format and overwrite status line to keep CLI output clean
            status_line = f"[{idx}/{total_payloads}] Testing: {name} (Payload: {format_bytes_hex(payload)[:25]}...)"
            padded_line = f"\r{status_line:<79}"
            sys.stdout.write(padded_line)
            sys.stdout.flush()
            
            # Write phase
            try:
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                ser.write(payload)
                ser.flush()
            except (serial.SerialException, OSError) as e:
                # Connection dropped during write (e.g. device reset / bootloader entry)
                sys.stdout.write("\n")
                print(f"{color('[!]', Colors.WARNING)} Connection lost immediately after sending {color(name, Colors.BOLD)}: {e}")
                print(f"    Payload (Hex): {format_bytes_hex(payload)}")
                hits += 1
                ser = wait_and_reopen(port, baud, timeout)
                continue
                
            # Wait for MCU to process the command
            time.sleep(delay)
            
            # Query incoming buffer size
            try:
                in_waiting = ser.in_waiting
            except (serial.SerialException, OSError) as e:
                sys.stdout.write("\n")
                print(f"{color('[!]', Colors.WARNING)} Connection lost after delay for {color(name, Colors.BOLD)}: {e}")
                print(f"    Payload (Hex): {format_bytes_hex(payload)}")
                hits += 1
                ser = wait_and_reopen(port, baud, timeout)
                continue
                
            # Read phase if data is present
            if in_waiting > 0:
                try:
                    response = ser.read(in_waiting)
                except (serial.SerialException, OSError) as e:
                    sys.stdout.write("\n")
                    print(f"{color('[!]', Colors.WARNING)} Connection lost during read for {color(name, Colors.BOLD)}: {e}")
                    print(f"    Payload (Hex): {format_bytes_hex(payload)}")
                    hits += 1
                    ser = wait_and_reopen(port, baud, timeout)
                    continue
                    
                hits += 1
                # Clear status line before printing the hit details
                sys.stdout.write("\r" + " " * 79 + "\r")
                
                print(f"\n{color('[+]', Colors.GREEN + Colors.BOLD)} RESPONSIVE PAYLOAD DISCOVERED!")
                print(f"    Payload Name: {color(name, Colors.CYAN)}")
                print(f"    Sent (Hex):   {color(format_bytes_hex(payload), Colors.BOLD)}")
                print(f"    Sent (ASCII): {format_bytes_ascii(payload)}")
                print(f"    Recv (Hex):   {color(format_bytes_hex(response), Colors.GREEN)}")
                print(f"    Recv (ASCII): {format_bytes_ascii(response)}")
                
                # Attempt to decode as UTF-8 gracefully
                try:
                    decoded = response.decode('utf-8')
                    print(f"    Recv (UTF-8): {decoded.strip()}")
                except UnicodeDecodeError:
                    pass
                
                print("-" * 50)
                
                if pause_on_hit:
                    input(f"\nPress {color('[Enter]', Colors.BOLD)} to resume fuzzing...")
                    # Clear buffers post-resume to discard any residual/echoed bytes
                    try:
                        ser.reset_input_buffer()
                        ser.reset_output_buffer()
                    except (serial.SerialException, OSError):
                        pass
                    
        print(f"\n\n{color('[*]', Colors.GREEN)} Fuzzing complete. Found {hits} responsive payload(s).")
        
    except KeyboardInterrupt:
        print(f"\n\n{color('[!]', Colors.WARNING)} Fuzzing aborted by user.")
    finally:
        if ser.is_open:
            ser.close()
            print(f"{color('[*]', Colors.BLUE)} Serial port closed.")

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fuzz a USB Virtual COM port to discover activation commands for air quality monitors.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-p", "--port",
        default="/dev/ttyACM0",
        help="Target serial/USB port device path"
    )
    parser.add_argument(
        "-b", "--baud",
        type=int,
        default=9600,
        help="Baud rate of the device connection"
    )
    parser.add_argument(
        "-t", "--timeout",
        type=float,
        default=1.0,
        help="Serial port read timeout in seconds"
    )
    parser.add_argument(
        "-d", "--delay",
        type=float,
        default=0.5,
        help="Wait time (seconds) after sending payload to check for response"
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Pause on each successful hit and wait for user to press Enter"
    )
    
    args = parser.parse_args()
    
    run_fuzzer(
        port=args.port,
        baud=args.baud,
        timeout=args.timeout,
        delay=args.delay,
        pause_on_hit=args.interactive
    )

if __name__ == "__main__":
    main()
