import socket
import json
import time
import sys

def request_approval():
    print("[\033[94mAgent\033[0m] Analyzing codebase...")
    time.sleep(1)
    print("[\033[94mAgent\033[0m] Need permission to modify src/auth/middleware.ts")
    
    payload = {
        "state": "approval",
        "agent": "Claude Code",
        "task": "Edit File",
        "targetFile": "src/auth/middleware.ts"
    }
    
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect(('127.0.0.1', 14321))
            s.sendall((json.dumps(payload) + "\n").encode('utf-8'))
            print("[\033[94mAgent\033[0m] Sent request to Vibe Island. \033[93mWaiting for user response in the Notch...\033[0m")
            
            # Wait for response
            data = s.recv(1024)
            if data:
                response = json.loads(data.decode('utf-8').strip())
                action = response.get("action")
                if action == "allow":
                    print("[\033[94mAgent\033[0m] \033[92m✅ Permission GRANTED by user.\033[0m Modifying file...")
                elif action == "deny":
                    print("[\033[94mAgent\033[0m] \033[91m❌ Permission DENIED by user.\033[0m Aborting task.")
                else:
                    print(f"[\033[94mAgent\033[0m] User selected: {action}")
            else:
                print("[\033[94mAgent\033[0m] Connection closed by Vibe Island.")
    except Exception as e:
        print(f"[\033[94mAgent\033[0m] Error: {e}")

if __name__ == "__main__":
    request_approval()
