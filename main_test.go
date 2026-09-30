package main

import (
	"bytes"
	"testing"
)

func head(r []byte, n int) []byte { return r[:n] }

func TestCurvePayloadMatchesDriverTemplate(t *testing.T) {
	// set_rpm_speed_cmd_template from waterforce-hwmon with be16(1500)=05 DC at offsets 5, 8, 11, 14
	want := []byte{0x99, 0xE6, 0x01, 0x01, 0, 0x05, 0xDC, 0x1E, 0x05, 0xDC, 0x32, 0x05, 0xDC, 0x41, 0x05, 0xDC}
	got := buildCurvePayload(0x01, 0x01, 1500)
	if !bytes.Equal(got[:16], want) || len(got) != 6144 || bytes.Count(got[16:], []byte{0}) != 6144-16 {
		t.Fatalf("got % X", got[:20])
	}
}

func TestCoolingPayloads(t *testing.T) {
	if n := len(buildCoolingPayloads(-1, -1)); n != 0 {
		t.Fatalf("untouched must send nothing, got %d", n)
	}
	r := buildCoolingPayloads(1200, 2700)
	if len(r) != 4 ||
		!bytes.Equal(head(r[0], 4), []byte{0x99, 0xE5, 0x01, 0x01}) ||
		!bytes.Equal(head(r[1], 7), []byte{0x99, 0xE6, 0x01, 0x01, 0, 0x04, 0xB0}) ||
		!bytes.Equal(head(r[2], 4), []byte{0x99, 0xE5, 0x02, 0x01}) ||
		!bytes.Equal(head(r[3], 7), []byte{0x99, 0xE6, 0x04, 0x02, 0, 0x0A, 0x8C}) {
		t.Fatal("manual fan + pump")
	}
	r = buildCoolingPayloads(0, 0)
	if len(r) != 2 || !bytes.Equal(head(r[0], 4), []byte{0x99, 0xE5, 0x01, 0x05}) || !bytes.Equal(head(r[1], 4), []byte{0x99, 0xE5, 0x02, 0x00}) {
		t.Fatal("factory profiles")
	}
}

func TestEnvValidation(t *testing.T) {
	for _, c := range []struct {
		fan, pump    string
		wantF, wantP int
	}{
		{"", "", -1, -1},
		{"0", "0", 0, 0},
		{"1200", "2700", 1200, 2700},
		{"700", "1500", -1, -1},
		{"2800", "3300", -1, -1},
		{"x", "balanced", -1, -1},
	} {
		t.Setenv("AWF_FAN_RPM", c.fan)
		t.Setenv("AWF_PUMP_RPM", c.pump)
		loadEnvConfig()
		if fanRPM != c.wantF || pumpRPM != c.wantP {
			t.Errorf("%q/%q: got %d/%d", c.fan, c.pump, fanRPM, pumpRPM)
		}
	}
}
