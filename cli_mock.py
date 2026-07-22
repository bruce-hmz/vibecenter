import socket
import json
import time
import sys

def send_payload(payload):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect(('127.0.0.1', 14321))
            s.sendall(json.dumps(payload).encode('utf-8'))
            print(f"Sent: {payload['state']}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "idle":
        payload = {
            "state": "compact",
            "agent": "Claude",
            "task": "Thinking...",
            "targetFile": ""
        }
    else:
        payload = {
            "state": "approval",
            "agent": "Claude Code",
            "task": "Command Execution",
            "targetFile": "npm run build"
        }
    send_payload(payload)
