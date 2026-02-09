import json
import os
from typing import List, Dict, Any

class EventLogger:
    def __init__(self, max_events: int = 100):
        self.max_events = max_events
        self.files = {
            "VAS": "vas_events.json",
            "POS": "pos_events.json"
        }
        self._initialize_files()

    def _initialize_files(self):
        for filepath in self.files.values():
            if not os.path.exists(filepath):
                with open(filepath, 'w') as f:
                    json.dump([], f)

    def log_event(self, stream_type: str, event_data: Dict[str, Any]):
        """Logs an event to the corresponding JSON file."""
        if stream_type not in self.files:
            return

        filepath = self.files[stream_type]
        
        try:
            # Read existing
            try:
                with open(filepath, 'r') as f:
                    events = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                events = []
            
            # Append new event
            events.append(event_data)
            
            # Trim
            if len(events) > self.max_events:
                events = events[-self.max_events:]
            
            # Write back
            with open(filepath, 'w') as f:
                json.dump(events, f, indent=2)
                
        except Exception as e:
            print(f"Error logging event to {filepath}: {e}")

    def get_events(self, stream_type: str, count: int = 20) -> List[Dict[str, Any]]:
        """Reads the last 'count' events from the file."""
        # Map stream names to types if needed, or just use types
        # API requests 'vas_stream' or 'pos_stream'
        
        type_key = None
        if stream_type == "vas_stream":
            type_key = "VAS"
        elif stream_type == "pos_stream" or stream_type == "sales_stream":
            type_key = "POS"
            
        if not type_key or type_key not in self.files:
            return []
            
        filepath = self.files[type_key]
        
        try:
            with open(filepath, 'r') as f:
                events = json.load(f)
                
            # Return last 'count' events, reversed (newest first)
            return list(reversed(events))[:count]
            
        except Exception as e:
            print(f"Error reading events from {filepath}: {e}")
            return []
