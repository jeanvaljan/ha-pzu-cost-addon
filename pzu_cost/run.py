import yaml
import time
from pathlib import Path

cfg = yaml.safe_load(Path("config.yaml").read_text())

def main():
    print("Loaded config:", cfg)
    while True:
        # placeholder for PZU cost logic
        time.sleep(cfg.get("pzu", {}).get("interval", 60))

if __name__ == "__main__":
    main()
