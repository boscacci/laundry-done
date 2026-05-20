# Amazon Parts Guide

This assumes you already have the ESP32 DevKit and a 5000 mAh magnetic USB power
bank. New purchases should stay under about $20 if you buy only the sensor and
basic build supplies.

Amazon listings move around, so prefer the search links and match the criteria
instead of chasing one exact seller.

## Buy This First

| Part | What To Search | Notes |
| --- | --- | --- |
| LSM6DS3 accelerometer/gyro breakout | [Amazon: LSM6DS3 accelerometer gyroscope I2C breakout](https://www.amazon.com/s?k=LSM6DS3+accelerometer+gyroscope+I2C+breakout) | This matches the GODIYMODULES purple board used in the current build. Plain header holes are easiest. |
| LIS3DH accelerometer breakout | [Amazon: LIS3DH accelerometer breakout I2C](https://www.amazon.com/s?k=LIS3DH+accelerometer+breakout+I2C) | Also supported. Choose this if LSM6DS3 listings are unavailable or overpriced. |
| Female-to-female Dupont jumpers | [Amazon: female female dupont jumper wires](https://www.amazon.com/s?k=female+female+dupont+jumper+wires) | Useful for the first bench test before soldering. |
| Short USB cable | [Amazon: 6 inch USB cable right angle](https://www.amazon.com/s?k=6+inch+usb+c+right+angle+cable) | Match your power bank output and ESP32 input. Short and strain-relieved is better than fancy. |
| Small project enclosure | [Amazon: small plastic electronics project box](https://www.amazon.com/s?k=small+plastic+electronics+project+box) | Optional but nicer than hot-gluing bare boards. Pick one just big enough for the ESP32 and accelerometer. |

## Nice To Have

| Part | What To Search | Why |
| --- | --- | --- |
| Heat-shrink tubing kit | [Amazon: heat shrink tubing assortment](https://www.amazon.com/s?k=heat+shrink+tubing+assortment) | Strain relief and insulated solder joints. |
| 3M Dual Lock or heavy hook-and-loop | [Amazon: 3M Dual Lock small strip](https://www.amazon.com/s?k=3M+Dual+Lock+small+strip) | Better than relying on a dangling hook; lets the enclosure stay flat against the appliance. |
| JST or screw-terminal pigtails | [Amazon: JST connector pigtail 2 pin](https://www.amazon.com/s?k=JST+connector+pigtail+2+pin) | Optional removable connection if you want to separate the lid/enclosure from the sensor. |

## Do Not Buy For V1

- A mains power supply or anything that touches the dryer outlet.
- A random “vibration switch” module as the main sensor. They are cheap, but the
  threshold is harder to tune and the signal is less useful than an
  accelerometer.
- Piezo/vibration harvesting boards. Fun later, but not needed for a reliable
  first build.

## Budget Notes

- Required new part: one supported I2C accelerometer breakout, preferably the
  LSM6DS3 board already tested in this build.
- Likely useful consumables: heat-shrink and short jumpers.
- Reuse: ESP32, USB power bank, existing magnets, soldering iron, hot glue.

If Amazon sensor options are overpriced, buy the accelerometer from Adafruit or
Digi-Key and get the generic supplies from Amazon. The firmware supports both
LSM6DS3-family boards and LIS3DH boards.
