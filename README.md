# Aorus WaterForce 360 cooler HID updater for Linux
## (°C, GHz, %CPU)

***
### By: fourgl@gmail.com 2026
***

## Dependencies:
   `go get github.com/karalabe/hid` 

## Build:
  * Configure your vars of the `main.go` if needed
  * `go build -o AorusWaterForce360-linux main.go`

## Installation:
  1. Copy the binary to `/usr/local/bin/AorusWaterForce360-linux`
  2. Copy configuration file to `/etc/systemd/system/AorusWaterForce360-linux.service`
  3. Enable and start the service:
	`sudo systemctl daemon-reload`
  `sudo systemctl enable AorusWaterForce360-linux`
	`sudo systemctl start AorusWaterForce360-linux`
  4. Check status:
	`systemctl status AorusWaterForce360-linux`

## Device:
  ID `1044:7a4d` Chu Yuen Enterprise Co., Ltd Castor3

## Tested on:
  AMD 7700X with Arch Linux (BTW :))

## Have fun!
