import pyaudio
import curses
import yaml
import os
import sys

def get_devices():
    p = pyaudio.PyAudio()
    devices = []
    
    # Temporarily suppress ALSA errors so they don't corrupt the curses display
    import ctypes
    ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
    def py_error_handler(filename, line, function, err, fmt):
        pass
    c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
    try:
        asound = ctypes.cdll.LoadLibrary('libasound.so.2')
        asound.snd_lib_error_set_handler(c_error_handler)
    except OSError:
        try:
            asound = ctypes.cdll.LoadLibrary('libasound.so')
            asound.snd_lib_error_set_handler(c_error_handler)
        except OSError:
            pass

    for i in range(p.get_device_count()):
        try:
            dev_info = p.get_device_info_by_index(i)
            if dev_info.get('maxInputChannels', 0) > 0:
                devices.append({
                    'index': i,
                    'name': dev_info.get('name'),
                    'channels': dev_info.get('maxInputChannels')
                })
        except Exception:
            continue
    p.terminate()
    return devices

def menu(stdscr, devices):
    curses.curs_set(0)
    current_row = 0
    
    # Allow selecting "null" (default device) as the first option
    options = [{'index': None, 'name': 'Default System Device', 'channels': '-'}] + devices

    while True:
        stdscr.clear()
        stdscr.addstr(0, 0, "Select the audio input device for SmartFrame (use UP/DOWN arrows, ENTER to select):", curses.A_BOLD)
        
        for idx, option in enumerate(options):
            x = 2
            y = 2 + idx
            
            idx_str = str(option['index']) if option['index'] is not None else "null"
            text = f"[{idx_str}] {option['name']} (Channels: {option['channels']})"
            
            if idx == current_row:
                stdscr.addstr(y, x, text, curses.A_REVERSE)
            else:
                stdscr.addstr(y, x, text)
                
        stdscr.refresh()
        
        key = stdscr.getch()
        
        if key == curses.KEY_UP and current_row > 0:
            current_row -= 1
        elif key == curses.KEY_DOWN and current_row < len(options) - 1:
            current_row += 1
        elif key == curses.KEY_ENTER or key in [10, 13]:
            return options[current_row]['index']
        elif key == 27:  # ESC Key
            return -1

def update_config(selected_index):
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.yaml')
    if not os.path.exists(config_path):
        print(f"Error: {config_path} not found!")
        return

    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}

        if 'audio' not in config:
            config['audio'] = {}

        config['audio']['device_index'] = selected_index
        
        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
            
        idx_str = str(selected_index) if selected_index is not None else "null"
        print(f"Success! Updated config.yaml: audio.device_index set to {idx_str}")
        
    except Exception as e:
        print(f"Error updating config.yaml: {e}")

def main():
    devices = get_devices()
    if not devices:
        print("No hardware audio input devices found! Please check your hardware or ALSA setup.")
        print("Falling back to System Default...")
    
    try:
        selected_index = curses.wrapper(menu, devices)
        if selected_index != -1: # Not cancelled
            update_config(selected_index)
        else:
            print("Selection cancelled. config.yaml was not changed.")
    except Exception as e:
        # Fallback to simple text mode if curses fails
        print("\nAvailable Audio Input Devices:")
        print("null: Default System Device")
        for d in devices:
            print(f"{d['index']}: {d['name']} (Channels: {d['channels']})")
        
        try:
            inp = input("Enter the device index number (or 'null' for default): ")
            if inp.strip().lower() == 'null':
                update_config(None)
            elif inp.strip().isdigit():
                update_config(int(inp.strip()))
        except KeyboardInterrupt:
            print("\nCancelled.")

if __name__ == "__main__":
    main()
