/*
Aorus WaterForce 360 cooler HID updater for Linux
(°C, GHz, %CPU)

By: fourgl@gmail.com 2026

Dependencies:
  none (writes to /dev/hidraw directly)

Build:
  1. Configure your vars of this file if needed
  2. go build -o AorusWaterForce360-linux main.go

Installation:
  sudo ui/install.sh   (daemon, systemd unit, control panel; see README.md)

Configuration (systemd drop-in, written by the control panel):
  AWF_SENSOR=Tccd1  AWF_INTERVAL=5  AWF_FAN_RPM=0|750..2750  AWF_PUMP_RPM=0|1600..3200

Device:
  ID 1044:7a4d Chu Yuen Enterprise Co., Ltd Castor3

Tested on:
  AMD 7700X with Arch Linux (BTW :))

Have fun!
*/

package main

import (
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var debugMode = false
var refreshInterval = 5 * time.Second
var sensorLabel = "Tccd1" // Tccd1 OR Tctl OR "" for first located (Tccd1 is more precise, may be Tccd2 makes sense for some systems?)
var sensorPath = ""       // to be detected

// Cooling: -1 = don't touch, 0 = back to the factory profile, otherwise a fixed RPM
var fanRPM = -1
var pumpRPM = -1

// Ranges checked on a WaterForce 360 (1044:7a4d): fan reached 2700 at 100% duty, pump 3290 at 100%;
// the pump is kept at 1600+ (lowest value tested) so it never starves the loop.
const minFanRPM, maxFanRPM = 750, 2750
const minPumpRPM, maxPumpRPM = 1600, 3200

var supportedDevices = []struct{ vid, pid uint16 }{
	{0x1044, 0x7A4D},
}

// AWF_* variables override the defaults above (set by the UI via a systemd drop-in)
func loadEnvConfig() {
	if v := os.Getenv("AWF_SENSOR"); v != "" {
		sensorLabel = v
	}
	if v, err := strconv.Atoi(os.Getenv("AWF_INTERVAL")); err == nil && v > 0 {
		refreshInterval = time.Duration(v) * time.Second
	}
	fanRPM = envRPM("AWF_FAN_RPM", minFanRPM, maxFanRPM)
	pumpRPM = envRPM("AWF_PUMP_RPM", minPumpRPM, maxPumpRPM)
}

func envRPM(name string, min, max int) int {
	v, err := strconv.Atoi(os.Getenv(name))
	if err != nil || (v != 0 && (v < min || v > max)) {
		return -1
	}
	return v
}

// hidraw node of the device: writing there keeps the kernel driver (gigabyte_waterforce) bound,
// so fan/pump RPM and coolant temperature stay available in hwmon
func findHidraw(vid, pid uint16) string {
	want := fmt.Sprintf("HID_ID=0003:%08X:%08X", vid, pid)
	nodes, _ := os.ReadDir("/sys/class/hidraw/")
	for _, n := range nodes {
		b, _ := os.ReadFile("/sys/class/hidraw/" + n.Name() + "/device/uevent")
		if strings.Contains(string(b), want) {
			return "/dev/" + n.Name()
		}
	}
	return ""
}

func readFirstLine(path string) string {
	b, _ := os.ReadFile(path)
	s := strings.TrimSpace(string(b))
	if s == "" {
		return ""
	}
	return s
}

func getCpuFrequency() int {
	b, err := os.ReadFile("/proc/cpuinfo")
	if err != nil {
		return 0
	}
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, "cpu MHz") {
			parts := strings.SplitN(l, ":", 2)
			if len(parts) == 2 {
				f, _ := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
				return int(f)
			}
		}
	}
	return 0
}

func getSensorPath(sensorLabel string) string {
	dirs, _ := os.ReadDir("/sys/class/hwmon/")
	for _, d := range dirs {
		p := "/sys/class/hwmon/" + d.Name() + "/name"
		name := readFirstLine(p)
		if strings.Contains(name, "k10temp") || strings.Contains(name, "coretemp") {
			files, _ := os.ReadDir("/sys/class/hwmon/" + d.Name())
			for _, f := range files {
				if strings.HasPrefix(f.Name(), "temp") && strings.HasSuffix(f.Name(), "_label") {
					v := readFirstLine("/sys/class/hwmon/" + d.Name() + "/" + f.Name())
					if v == sensorLabel || sensorLabel == "" {
						if debugMode {
							fmt.Printf("Found temperature sensor %s at %s\n", v, d.Name())
						}
						return "/sys/class/hwmon/" + d.Name() + "/" + strings.Replace(f.Name(), "_label", "_input", 1)
					}
				}
			}
		}
	}
	return ""
}

func getCpuTemperature(sensorPath string) int {
	if sensorPath == "" {
		return 0
	}
	v := readFirstLine(sensorPath)
	if v != "" {
		i, _ := strconv.Atoi(strings.TrimSpace(v))
		return i / 1000
	}
	return 0
}

func getCpuUsage() int {
	idle1, total1 := cpuTimes()
	time.Sleep(500 * time.Millisecond)
	idle2, total2 := cpuTimes()
	idle := idle2 - idle1
	total := total2 - total1
	if total == 0 {
		return 0
	}
	usage := 100 * (1.0 - float64(idle)/float64(total))
	return int(usage)
}

func cpuTimes() (idle, total uint64) {
	b, err := os.ReadFile("/proc/stat")
	if err != nil {
		return 0, 1
	}
	for _, l := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(l, "cpu ") {
			parts := strings.Fields(l)
			var vals []uint64
			for i := 1; i < len(parts); i++ {
				v, _ := strconv.ParseUint(parts[i], 10, 64)
				vals = append(vals, v)
			}
			if len(vals) > 3 {
				idle = vals[3]
				for _, v := range vals {
					total += v
				}
			}
			break
		}
	}
	return
}

func buildPayload(cTemp, cFreq, cUsage int) []byte {
	buf := make([]byte, 6144)
	buf[0] = 153
	buf[1] = 224
	//buf[2] = 0
	buf[3] = byte(cTemp)
	buf[4] = 16
	buf[5] = byte(cFreq / 1000)
	buf[6] = byte((cFreq / 100) % 10)
	buf[7] = 8
	buf[8] = 24
	//buf[9] = 0
	buf[10] = byte(cUsage)
	//buf[11] = 0
	//buf[12] = 0
	return buf
}

func newReport(cmd ...byte) []byte {
	buf := make([]byte, 6144)
	copy(buf, cmd)
	return buf
}

// Cooling protocol, verified on the device by probing (see ui/probe*.py):
//
//	E5 01 <preset>   fan profile:  01 = custom curve, 05 = factory default
//	E5 02 <preset>   pump profile: 01 = custom curve, 00 = factory default (02, 04 = faster presets)
//	E6 <ch> <ch> + 4 × [temp °C][RPM be16]   curve for channel 01 01 (fan) or 04 02 (pump)
//
// The curve layout comes from the waterforce-hwmon driver by Aleksa Savic (pre-mainline version).
// The device stores curves and profiles itself, so they are sent once per start, not every cycle.
func buildCurvePayload(ch1, ch2 byte, rpm int) []byte {
	buf := newReport(0x99, 0xE6, ch1, ch2)
	for i, temp := range []byte{0, 30, 50, 65} {
		buf[4+i*3] = temp
		buf[5+i*3] = byte(rpm >> 8)
		buf[6+i*3] = byte(rpm)
	}
	return buf
}

func buildCoolingPayloads(fanRPM, pumpRPM int) [][]byte {
	var reports [][]byte
	switch {
	case fanRPM == 0:
		reports = append(reports, newReport(0x99, 0xE5, 0x01, 0x05))
	case fanRPM > 0:
		reports = append(reports, newReport(0x99, 0xE5, 0x01, 0x01), buildCurvePayload(0x01, 0x01, fanRPM))
	}
	switch {
	case pumpRPM == 0:
		reports = append(reports, newReport(0x99, 0xE5, 0x02, 0x00))
	case pumpRPM > 0:
		reports = append(reports, newReport(0x99, 0xE5, 0x02, 0x01), buildCurvePayload(0x04, 0x02, pumpRPM))
	}
	return reports
}

func main() {

	loadEnvConfig()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)

	for _, d := range supportedDevices {
		path := findHidraw(d.vid, d.pid)
		if path == "" {
			if debugMode {
				fmt.Printf("No HID device %04x:%04x\n", d.vid, d.pid)
			}
			continue
		}
		if debugMode {
			fmt.Printf("Connecting to HID device %04x:%04x at %s\n", d.vid, d.pid, path)
		}
		device, err := os.OpenFile(path, os.O_WRONLY, 0)
		if err != nil {
			fmt.Printf("Open HID device %04x:%04x failed: %s\n", d.vid, d.pid, err)
			os.Exit(1)
		} else {
			if debugMode {
				fmt.Printf("HID device %04x:%04x opened\n", d.vid, d.pid)
			}
		}

		defer device.Close()

		for _, r := range buildCoolingPayloads(fanRPM, pumpRPM) {
			if _, err := device.Write(r); err != nil {
				fmt.Println("Write to HID device failed:", err)
				os.Exit(1)
			}
		}

		sensorPath := getSensorPath(sensorLabel)

		for {
			select {
			case <-sig:
				if debugMode {
					fmt.Println("Shutting down")
				}
				return
			default:
				cTemp := getCpuTemperature(sensorPath)
				cFreq := getCpuFrequency()
				cUsage := getCpuUsage()
				buf := buildPayload(cTemp, cFreq, cUsage)
				_, err := device.Write(buf)
				if err != nil {
					// device was replugged: exit so systemd restarts us on the new hidraw node
					fmt.Println("Write to HID device failed:", err)
					os.Exit(1)
				}
				if debugMode {
					fmt.Printf("%s %d°C %d.%dGHz %d%% fan=%d pump=%d\n", ">>", cTemp, cFreq/1000, (cFreq/100)%10, cUsage, fanRPM, pumpRPM)
				}
				time.Sleep(refreshInterval)
			}
		}
	}
	fmt.Println("No supported HID device found")
	os.Exit(1)
}
