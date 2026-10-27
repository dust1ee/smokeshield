# SmokeShield setup

Pi 3 B+ · Raspberry Pi OS (Debian 13 "trixie") · PMS5003 · Hiwonder HPS-2018MG servo · 12 V battery · 2 × LM2596 buck converters

Total time: about 2–3 hours, plus calibration.

## Files

| File | Goes where |
| --- | --- |
| `index.html` | GitHub Pages (phone page) |
| `smokeshield.py` | Pi: `/opt/smokeshield/` |
| `smokeshield.service` | Pi: `/etc/systemd/system/` |

Easiest way to get them onto the Pi: put all three in the same GitHub repo as the page, then clone it on the Pi (step 3).

---

## 1. Set the buck converters (10 min, BEFORE wiring anything else)

1. Connect the battery to Buck 1's input only.
2. Turn its screw until the display reads **5.0 V** on the output. Disconnect.
3. Same for Buck 2: set it to **7.5 V**. Disconnect.

A buck set wrong can kill the Pi. Check both with a multimeter if you have one.

## 2. Wiring (30 min)

```
                ┌──► Buck 1 (5.0 V) ──► Pi pin 2 (+) / pin 6 (−)
12 V battery ───┤
                └──► Buck 2 (7.5 V) ──► Servo + / −  (− also to Pi pin 14)

Pi pin 12 ──────────► Servo signal
Pi pin 4 / 9 ───────► PMS5003 power (5 V / GND)
Pi pin 10 ◄───────── PMS5003 TX     Pi pin 8 ──► PMS5003 RX
```

| From | To |
| --- | --- |
| Battery + | Buck 1 IN+ and Buck 2 IN+ |
| Battery − | Buck 1 IN− and Buck 2 IN− |
| Buck 1 OUT+ (5.0 V) | Pi **pin 2** (5V) |
| Buck 1 OUT− | Pi **pin 6** (GND) |
| Buck 2 OUT+ (7.5 V) | Servo **+** |
| Buck 2 OUT− | Servo **−** and Pi **pin 14** (GND) |
| Servo signal | Pi **pin 12** (GPIO18) |
| PMS5003 VCC (5 V) | Pi **pin 4** (5V) |
| PMS5003 GND | Pi **pin 9** (GND) |
| PMS5003 TX | Pi **pin 10** (GPIO15, RX) |
| PMS5003 RX | Pi **pin 8** (GPIO14, TX) |

- **Servo wire colors:** check Hiwonder's label or sheet for which wire is signal, +, −. Don't go by color alone.
- **PMS5003 pins:** check the pin labels on your adapter board. Leave SET and RESET unconnected. If the sensor sends nothing, connect SET to Pi pin 1 (3.3 V).
- **If the PM2.5 adapter is USB instead:** plug it into a USB port, skip the 4 PMS5003 rows, and set `PMS_PORT = "/dev/ttyUSB0"` in `smokeshield.py`.
- **Recommended:** an inline fuse (about 5 A) on Battery +.
- Pin 2 feeds the Pi directly, skipping its input fuse. Double-check polarity before powering on.

## 3. Pi software (30 min)

Run on the Pi, one line at a time:

```bash
sudo apt update
sudo apt install -y git python3-dbus python3-gi python3-serial
git clone https://github.com/YOUR-NAME/YOUR-REPO.git ~/smokeshield-src
sudo mkdir -p /opt/smokeshield
sudo cp ~/smokeshield-src/smokeshield.py /opt/smokeshield/
sudo python3 -m venv --system-site-packages /opt/smokeshield/venv
sudo /opt/smokeshield/venv/bin/pip install --no-deps bluezero
```

Replace `YOUR-NAME/YOUR-REPO` with your GitHub repo.

## 4. Pi settings (10 min)

1. `sudo raspi-config` → **Interface Options** → **Serial Port**
   - Login shell over serial? **No**
   - Serial port hardware enabled? **Yes**
2. `sudo nano /boot/firmware/config.txt`
   - Change `dtparam=audio=on` to `dtparam=audio=off`
   - Add at the very end: `dtoverlay=pwm,pin=18,func=2`
   - Save: Ctrl+O, Enter, Ctrl+X
3. `sudo reboot`

(Skip step 1 if the sensor is on USB.)

## 5. Bench tests (30 min)

**Sensor:**

```bash
sudo /opt/smokeshield/venv/bin/python /opt/smokeshield/smokeshield.py --sensor-test
```

Expect a PM2.5 number every 2 s. Blow out a match near it: the number should jump. Ctrl+C to stop.

**Servo** (flap NOT attached yet):

```bash
sudo /opt/smokeshield/venv/bin/python /opt/smokeshield/smokeshield.py --servo-test 1500
```

Press Enter, then repeat with other numbers in steps of 100 (1400, 1600, …). Write down the two numbers that give **open** and **closed** (90° apart). If the servo buzzes or strains, you've gone past its end: back off.

Put them in `smokeshield.py`:

```bash
sudo nano /opt/smokeshield/smokeshield.py
```

Set `SERVO_OPEN_US` and `SERVO_CLOSED_US`.

## 6. Full run + phone (15 min)

```bash
sudo /opt/smokeshield/venv/bin/python /opt/smokeshield/smokeshield.py
```

1. Wait for `Advertising as 'SmokeShield'`.
2. On the iPhone: open your page in **Bluefy** → **Connect** → pick **SmokeShield**.
3. Tap **Close shutter**, **Open shutter**, **Switch to AUTO**. The flap should follow.

## 7. Start automatically at power-on (5 min)

```bash
sudo cp ~/smokeshield-src/smokeshield.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now smokeshield
```

Check it: `systemctl status smokeshield` · Live log: `journalctl -u smokeshield -f`

After any change to `smokeshield.py`: `sudo systemctl restart smokeshield`

## 8. Calibrate smoke levels (1 h, on the duct rig)

The levels in `smokeshield.py` are **temporary test values, not from any standard**.

1. Run the box fan with clean air for 10 min. Watch `journalctl -u smokeshield -f` or the phone. Note the highest PM2.5.
2. Light incense upstream. Note PM2.5 with smoke.
3. Set `CLOSE_PM25` between the two numbers, and `CLEAR_PM25` a bit above clean air.
4. `sudo systemctl restart smokeshield` and test again.

| | PM2.5 (µg/m³) |
| --- | --- |
| Clean air, highest | ___ |
| With incense | ___ |
| `CLOSE_PM25` | ___ |
| `CLEAR_PM25` | ___ |

---

## How it behaves

| Situation | What happens |
| --- | --- |
| AUTO, PM2.5 ≥ `CLOSE_PM25` for 10 s | Flap closes |
| AUTO, PM2.5 < `CLEAR_PM25` for 5 min | Flap opens |
| Phone: Open/Close button | Flap moves, mode switches to MANUAL |
| Phone: Switch to AUTO / MANUAL | Mode changes |
| Sensor stops sending | Flap stays where it is; phone shows a warning |
| Pi starts up | Flap open, AUTO |

## If something doesn't work

| Symptom | Check |
| --- | --- |
| `No hardware PWM found` | Step 4.2 done? Rebooted? |
| Sensor test says `no data yet` | TX/RX swapped? Serial enabled (step 4.1)? Try SET → 3.3 V |
| Phone doesn't see SmokeShield | Program running? Bluetooth on: `bluetoothctl show` should say `Powered: yes` |
| Phone connects, no numbers | Open the page's Connection log, look for lines starting with `←` |
| Pi reboots when the servo moves | Servo must be on Buck 2, not on the Pi's 5 V pins |
