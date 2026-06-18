#!/usr/bin/env python3
"""
Modbus RTU Register Scanner for Temtop LKC-1000S+ 2nd Gen & Similar Devices.
Queries registers over serial, decodes standard Modbus responses and exceptions,
and presents multiple data conversions to help map air quality sensor values.
"""

import argparse
import struct
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
    """
    Parses a Modbus RTU response frame.
    Returns a tuple of (status_string, data_bytes).
    """
    if len(response) < 5:
        return "TIMEOUT/SHORT_RESPONSE", None
    
    slave_id = response[0]
    func_code = response[1]
    
    if slave_id != expected_slave:
        return f"SLAVE_MISMATCH (Expected {expected_slave}, got {slave_id})", None
    
    # Check for Modbus Exception (Function code has high bit set)
    if func_code == (expected_func | 0x80):
        exception_code = response[2]
        exc_names = {
            1: "Illegal Function (0x01)",
            2: "Illegal Data Address (0x02) - Register not present",
            3: "Illegal Data Value (0x03)",
            4: "Slave Device Failure (0x04)"
        }
        exc_desc = exc_names.get(exception_code, f"Unknown Exception ({exception_code})")
        return f"EXCEPTION: {exc_desc}", None
        
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

def interpret_value(raw_bytes: bytes) -> Dict[str, str]:
    """Provides multiple mathematical interpretations of 2-byte register data."""
    if len(raw_bytes) != 2:
        return {}
    
    val_unsigned = int.from_bytes(raw_bytes, byteorder='big', signed=False)
    val_signed = int.from_bytes(raw_bytes, byteorder='big', signed=True)
    
    # Scale interpretations
    div_10 = f"{val_signed / 10.0:.1f}"
    div_100 = f"{val_signed / 100.0:.2f}"
    div_1000 = f"{val_signed / 1000.0:.3f}"
    
    return {
        "raw_hex": raw_bytes.hex().upper(),
        "u16": str(val_unsigned),
        "s16": str(val_signed),
        "div_10": div_10,
        "div_100": div_100,
        "div_1000": div_1000
    }

def try_read_register(ser: serial.Serial, slave_id: int, func_code: int, register: int, timeout: float) -> Tuple[str, Optional[bytes]]:
    """Sends a read request for a single register and reads the response."""
    req = make_modbus_request(slave_id, func_code, register, quantity=1)
    
    # Clear buffers
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(req)
        ser.flush()
    except (serial.SerialException, OSError) as e:
        return f"WRITE_ERROR: {e}", None
        
    # Read response (expected Modbus RTU read reply size: 7 bytes)
    # slave(1) + func(1) + bytes(1) + data(2) + crc(2) = 7
    # For exception: slave(1) + func|0x80(1) + err(1) + crc(2) = 5
    # We wait up to timeout, then read whatever is in the buffer
    time.sleep(0.05) # short buffer wait
    start_time = time.time()
    
    response = b""
    while (time.time() - start_time) < timeout:
        if ser.in_waiting > 0:
            try:
                response += ser.read(ser.in_waiting)
            except (serial.SerialException, OSError):
                return "READ_ERROR", None
        # Break early if we got standard frame or exception frame
        if len(response) >= 7 or (len(response) >= 5 and (response[1] & 0x80)):
            break
        time.sleep(0.01)
        
    return parse_modbus_response(response, slave_id, func_code)

def scan_registers(port: str, baud: int, slave_id: int, funcs: List[int], start_reg: int, end_reg: int, delay: float, timeout: float) -> None:
    """Executes the register scanning loop and outputs results in a formatted table."""
    print(color("=== Temtop Modbus RTU Register Scanner ===", Colors.HEADER + Colors.BOLD))
    print(f"Target Port:  {color(port, Colors.CYAN)}")
    print(f"Baud Rate:    {color(str(baud), Colors.CYAN)}")
    print(f"Slave ID:     {color(f'{slave_id} (0x{slave_id:02X})', Colors.CYAN)}")
    print(f"Functions:    {color(', '.join(map(str, funcs)), Colors.CYAN)}")
    print(f"Reg Range:    {color(f'{start_reg} - {end_reg} (0x{start_reg:04X} - 0x{end_reg:04X})', Colors.CYAN)}")
    print("=" * 45 + "\n")
    
    try:
        ser = serial.Serial(port, baud, timeout=timeout)
        print(f"{color('[*]', Colors.GREEN)} Opened serial port.")
    except (serial.SerialException, OSError) as e:
        print(f"{color('[!]', Colors.FAIL)} Error opening port: {e}")
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if ports:
            print(f"Available ports: {', '.join(ports)}")
        sys.exit(1)
        
    successful_reads: List[Tuple[int, int, bytes, Dict[str, str]]] = []
    
    try:
        for func in funcs:
            print(f"\nScanning registers using {color(f'Function Code {func:02d}', Colors.BOLD + Colors.BLUE)}...")
            
            for reg in range(start_reg, end_reg + 1):
                # Update status
                status_line = f"Scanning Reg: {reg} (0x{reg:04X})..."
                sys.stdout.write(f"\r{status_line:<50}")
                sys.stdout.flush()
                
                status, data = try_read_register(ser, slave_id, func, reg, timeout)
                
                if status == "SUCCESS" and data is not None:
                    interpretations = interpret_value(data)
                    successful_reads.append((func, reg, data, interpretations))
                    
                    # Clear line and print immediate success
                    sys.stdout.write("\r" + " " * 50 + "\r")
                    print(f"{color('[+]', Colors.GREEN)} Found Register: {color(str(reg), Colors.BOLD)} (0x{reg:04X}) | Val: 0x{data.hex().upper()} ({int.from_bytes(data, 'big')})")
                elif "EXCEPTION" in status and "Illegal Data Address" not in status:
                    # Log exceptions other than "register not present"
                    sys.stdout.write("\r" + " " * 50 + "\r")
                    print(f"{color('[!]', Colors.WARNING)} Register {reg} (0x{reg:04X}) returned Exception: {status}")
                
                time.sleep(delay)
                
        # Clear the status line
        sys.stdout.write("\r" + " " * 50 + "\r")
        
        # Display Summary Table
        if successful_reads:
            print(f"\n{color('=== SCAN RESULTS SUMMARY ===', Colors.HEADER + Colors.BOLD)}")
            print(f"{'FC':<3} | {'Reg (Dec)':<9} | {'Reg (Hex)':<9} | {'Raw Hex':<7} | {'Unsig16':<8} | {'Sig16':<8} | {'val/10':<7} | {'val/100':<8} | {'val/1000'}")
            print("-" * 88)
            for func, reg, data, interp in successful_reads:
                print(f"{func:02d}  | {reg:<9} | 0x{reg:04X}   | {interp['raw_hex']:<7} | {interp['u16']:<8} | {interp['s16']:<8} | {interp['div_10']:<7} | {interp['div_100']:<8} | {interp['div_1000']}")
            
            print("\n" + "=" * 45)
            print(f"{color('[*]', Colors.GREEN)} Scan complete. Found {len(successful_reads)} active register(s).")
            print("To correlate these with your screen:")
            print("1. Compare the 'val/10' column for temperature (e.g. 22.3°C or 73.1°F).")
            print("2. Compare the 'Unsig16' column for PM2.5, PM10, AQI, and Particle count.")
            print("3. Compare 'val/100' or 'val/1000' for HCHO/TVOC (e.g. 0.02 mg/m3 -> 2 or 20).")
            
            # Offer bulk reading test
            print(f"\n{color('[*] Suggestion:', Colors.BOLD)} Try reading all registers in a single block to see if it supports streaming:")
            first_reg = successful_reads[0][1]
            last_reg = successful_reads[-1][1]
            block_sz = last_reg - first_reg + 1
            if block_sz <= 32:
                print(f"Run: python3 modbus_scanner.py --port {port} --baud {baud} --slave {slave_id} --bulk {first_reg}:{block_sz}")
        else:
            print(f"\n{color('[!]', Colors.WARNING)} No registers responded. Double check your slave address, baud rate, and connection.")
            
    except KeyboardInterrupt:
        print(f"\n\n{color('[!]', Colors.WARNING)} Scan aborted by user.")
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()
            print(f"{color('[*]', Colors.BLUE)} Serial port closed.")

def run_bulk_read(port: str, baud: int, slave_id: int, func_code: int, start_reg: int, quantity: int, timeout: float) -> None:
    """Attempts a bulk read of multiple consecutive registers and displays the decoded payload."""
    print(color(f"=== Modbus RTU Bulk Read (FC {func_code:02d}) ===", Colors.HEADER + Colors.BOLD))
    print(f"Register Address: {start_reg} (0x{start_reg:04X}) | Quantity: {quantity}")
    
    try:
        ser = serial.Serial(port, baud, timeout=timeout)
    except (serial.SerialException, OSError) as e:
        print(f"{color('[!]', Colors.FAIL)} Error opening port: {e}")
        sys.exit(1)
        
    try:
        req = make_modbus_request(slave_id, func_code, start_reg, quantity)
        print(f"Request (Hex):    {format_bytes_hex(req)}")
        
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(req)
        ser.flush()
        
        # Expected bytes: slave(1) + func(1) + bytes(1) + data(2*qty) + crc(2) = 5 + 2*qty
        expected_len = 5 + 2 * quantity
        time.sleep(0.08)
        
        response = b""
        start_time = time.time()
        while (time.time() - start_time) < timeout:
            if ser.in_waiting > 0:
                response += ser.read(ser.in_waiting)
            if len(response) >= expected_len or (len(response) >= 5 and (response[1] & 0x80)):
                break
            time.sleep(0.01)
            
        print(f"Response (Hex):   {format_bytes_hex(response)}")
        
        status, data = parse_modbus_response(response, slave_id, func_code)
        if status == "SUCCESS" and data is not None:
            print(f"\n{color('[+]', Colors.GREEN + Colors.BOLD)} Bulk Read Succeeded!")
            print(f"{'Offset':<6} | {'Reg (Dec)':<9} | {'Reg (Hex)':<9} | {'Raw Hex':<7} | {'Unsig16':<8} | {'Sig16':<8} | {'val/10':<7} | {'val/100':<8} | {'val/1000'}")
            print("-" * 88)
            for i in range(quantity):
                reg_addr = start_reg + i
                reg_data = data[i*2 : (i+1)*2]
                interp = interpret_value(reg_data)
                print(f"+{i*2:<5} | {reg_addr:<9} | 0x{reg_addr:04X}   | {interp['raw_hex']:<7} | {interp['u16']:<8} | {interp['s16']:<8} | {interp['div_10']:<7} | {interp['div_100']:<8} | {interp['div_1000']}")
                
            # Try to decode as floats (32-bit floats spanning 2 registers)
            if quantity >= 2:
                print(f"\n{color('[*] Checking for 32-bit Float values:', Colors.BOLD)}")
                for i in range(quantity - 1):
                    four_bytes = data[i*2 : (i+2)*2]
                    # big endian float
                    val_float_be = struct.unpack(">f", four_bytes)[0]
                    # little endian word swap float (CDAB)
                    word_swapped = four_bytes[2:4] + four_bytes[0:2]
                    val_float_sw = struct.unpack(">f", word_swapped)[0]
                    print(f"  Regs {start_reg+i}-{start_reg+i+1} : Float BE = {val_float_be:.5f} | Word-Swapped = {val_float_sw:.5f}")
        else:
            print(f"\n{color('[!]', Colors.FAIL)} Bulk Read Failed: {status}")
            
    except KeyboardInterrupt:
        print("\nAborted.")
    finally:
        ser.close()

def format_bytes_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan registers on a Modbus-RTU device to discover active sensors and parameter mappings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("-p", "--port", default="/dev/ttyACM0", help="Serial port device path")
    parser.add_argument("-b", "--baud", type=int, default=115200, help="Baud rate (default: 115200 for USB virtual COM ports, try 9600 for serial transceivers)")
    parser.add_argument("-s", "--slave", default="0xFE", help="Slave address to query (e.g. 1-247, or 0xFE, 254)")
    parser.add_argument("-f", "--funcs", default="3,4", help="Modbus function codes to scan (comma-separated, e.g. 3,4)")
    parser.add_argument("-r", "--range", default="0-32", help="Range of registers to scan (e.g. 0-64 or 1-20)")
    parser.add_argument("-d", "--delay", type=float, default=0.1, help="Delay in seconds between register queries")
    parser.add_argument("-t", "--timeout", type=float, default=0.5, help="Serial response timeout in seconds")
    parser.add_argument("--bulk", default="", help="Perform a bulk read test. Format: START_REG:QUANTITY (e.g. 0:10)")
    
    args = parser.parse_args()
    
    # Parse slave ID (handle hex or decimal strings)
    try:
        if args.slave.lower().startswith("0x"):
            slave_id = int(args.slave, 16)
        else:
            slave_id = int(args.slave)
    except ValueError:
        print(f"Invalid slave address format: {args.slave}")
        sys.exit(1)
        
    # Parse function codes
    try:
        funcs = [int(f.strip()) for f in args.funcs.split(",")]
    except ValueError:
        print(f"Invalid function codes: {args.funcs}")
        sys.exit(1)
        
    if args.bulk:
        try:
            start_str, qty_str = args.bulk.split(":")
            start_reg = int(start_str)
            qty = int(qty_str)
        except ValueError:
            print("Invalid bulk read format. Use START_REG:QUANTITY (e.g. 0:10)")
            sys.exit(1)
            
        # Default bulk read to the first function code specified
        run_bulk_read(args.port, args.baud, slave_id, funcs[0], start_reg, qty, args.timeout)
    else:
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
            
        scan_registers(args.port, args.baud, slave_id, funcs, start_reg, end_reg, args.delay, args.timeout)

if __name__ == "__main__":
    main()
