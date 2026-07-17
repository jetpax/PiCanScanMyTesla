# PiCanScanMyTesla - internals and bring-up notes

Working notes for the classic Tesla Model S 85/90 pack (2012-2016, penthouse
master BMS) on the bench. Covers CAN decoding, the diagnostic tooling, and the
long road to operating the contactors out of the car.

## Hardware / topology

- Pack LV connector **X036** (grey, MX150 12P-B). Pinout used:
  - pin 2  = KL_30 (+12 V, BMS 3 A supply)
  - pin 5  = Contactor_PWR (contactor coil supply, 10 A fused in car)
  - pin 6  = SW12V_Drive
  - pin 8  = GND-BMS
  - pin 9  = PT_CAN- (CAN L)
  - pin 10 = PT_CAN+ (CAN H)
  - pin 11 = HVIL Out
  - pin 12 = GND-BMS
- **X035** (black) mirrors PT_CAN for daisy-chaining; carries HVIL In and
  `#CONT_SERV_BYP` (contactor service bypass, unconnected in car, sits pulled
  up ~10 V - unproven, possible service-mode input, see open items).
- CAN is 500 kbit/s on X036 9/10. Adapter: RH02 CANable clone (slcan) on
  a Pi -> can0.

## CAN decode (all IDs the tool uses)

- `0x6F2` per-brick V + temps. byte0 = mux. mux 0-23 = four 14-bit brick
  voltages, LE bitstream over bytes 1-7, x0.000305 V (brick n = 4*mux + k).
  mux 24-31 = four 14-bit signed temps, x0.0122 C, 2 sensors/module.
  raw 0 / 0x3FFF = SNA. **All-FF payloads are a normal rotating invalidate
  cadence (one 8-mux block every ~10 s), NOT a dropout** - never clear cached
  values on them; a truly unreachable BMB stops sending data entirely.
- `0x102` pack V (u16 LE x0.01), current (s16 LE x0.1, slow/averaged).
- `0x302` SOC UI (byte0 + low 2 bits byte1, /10). Reads 0 on a resting bench
  pack.
- `0x212` BMS status:
  - d0 bit6 = hvilStatus; d1 bit3 = hvilOn; d1 & 0x07 = highestFaultCategory
  - d2 hi nibble = BMS state (0 STANDBY, 1 DRIVE, 2 SUPPORT, 6 CLEAR_FAULT,
    7 FAULT); d2 lo nibble = contactor state (1 open, 2 opening, 3 closing,
    4 closed, 5 welded, 6 blocked)
  - d3 = isolation, x20 kOhm (0xFF = SNA)
- `0x202` pack V limits (u16 LE x0.01). `0x332` BMS's own brick min/max
  (x2 mV) + temp min/max (d3,d7 x0.5-40) - cross-checks 0x6F2 exactly.
  `0x382` energy, 10-bit LE fields x0.1 kWh, field0 = nominal full (SoH).
  `0x3D2` lifetime charge (bytes 0-3) / discharge (bytes 4-7), u32 LE Wh.
  `0x562` odometer u32 LE x0.001 mi.

Fault meanings are from Tesla's own Fleet API `alert_dictionary.csv` (scoped
Model S/X 2012-2020). The routine-byte -> name mapping is Battery-Emulator's
hand annotation and has a known collision (routine byte 0x02 tagged BOTH f023
and f163), so names are best-effort; meanings are Tesla's.

## Two BMS behaviours worth knowing

- **HVIL source is CAN-activity gated.** The 20 mA interlock current source is
  fed from the **Contactor_PWR rail (X036 pin 5)**, not the BMS KL_30 supply,
  and only energises while there is bus traffic. With pin 5 unpowered the loop
  never energises regardless of how it is jumpered. With continuous 100 ms
  traffic, `hvilOn` reads closed; pause the traffic and it drops to open within
  seconds. HVIL is a single series loop through every HV component in the car,
  out X036/11 and back to X035/10 into an internal 60 Ohm terminator. On the
  bench it needs the rapid-mate pin bridged at X036 plus continuity 11 -> 10.
- **UDS reads fail while the vehicle heartbeats play** - the 100 ms frames
  corrupt the seed/key exchange. Pause "Play vehicle" to read/clear DTCs.

## Playing the car (getting the BMS to operate)

The classic pack sits in FAULT/standby until something plays the vehicle.
Battery-Emulator's TESLA-LEGACY driver sends five static frames, no counters,
no checksums, nothing conditional on contactor state:

- `0x21C` 31 58 20 89 8C 08 03 08   (charger status)
- `0x25C` 00 02 2A 09 40 C7 72 81   (fast-charge status)
- `0x2C8` 6F E8 13 71 1D 24 80 7B   (GTW / gateway status - the key one)
- `0x20E` 05 56 22 00 C3 00 02 08   (charge-port status)
- `0x408` 00                        (1 s keepalive)

21C/25C/2C8/20E at 100 ms, 0x408 at 1 s. teslamon sends exactly this via the
"Play vehicle" toggle. The newer TESLA-BATTERY.cpp (different, newer packs) is
far more involved - rolling counters + checksums and a 0x221 VCFRONT power-down
timer - but none of that applies to the classic pack.

## Bring-up sequence that got contactors to physically close

1. Cell/health diagnostics work with just the 5 s keepalive - this was the
   original goal (find the bad module) and it was complete early.
2. HVIL closes once **Contactor_PWR (pin 5) is powered** and bus traffic keeps
   the source energised.
3. **Isolation self-check only passes with the HV series string assembled** -
   with modules disconnected, iso reads SNA and the BMS faults a few seconds
   after STANDBY. Assembled: iso ~3.4-3.6 MOhm, pack ~372 V, and it holds
   STANDBY.
4. Contactor coils need a **stiff 12 V supply** - the pull-in inrush collapses
   a weak bench rail so the coil-driver relays click but the contactors never
   latch. A charged 12 V battery pulls them in and holds.

## Open item: contactors close then reopen into a lockout (bare terminals)

Symptom: POR -> STANDBY (~19 s, healthy) -> contactor CLOSING and FAULT in the
same 0x212 transition (category 1 -> 4) -> open 100 ms later -> a few
STANDBY<->FAULT retries -> latched lockout until next POR. Our heartbeat is
gap-free throughout (ruled out on the bus), so it is NOT a comms timeout and
NOT a missing car frame.

**Leading explanation: the pack needs a load on the HV output.** The internal
HVP runs a precharge sequence at contactor close and monitors the output
(DC-link) voltage. Into bare, floating terminals there is no defined
capacitance/load, so the reading is irrational and the BMS trips at the close
(cf. `BMS_a139_SW_DC_Link_V_Irrational` in the newer driver). This fits the
timeline exactly and is corroborated by Battery-Emulator having no bare-terminal
mode at all: its `precharge_control` always ramps into an inverter's DC-link
capacitance, gated on `inverter_allows_contactor_closing`. DBE is designed to
run packs out of the car but always with a load present.

Next test: present a defined load - a DC-link capacitor (few hundred uF, rated
>450 V) and/or a power resistor drawing a modest current - and retry. If it
holds, confirmed.

`f023` (SW_Contactors_Open_HWOC) was a red herring: a stored historical count,
set from the start, unclearable over UDS (the 0x02 routine collision means our
"clear f023" was clearing f163). It is not believed to be the blocker.

Other unexplored lead: `#CONT_SERV_BYP` on X035 (contactor service bypass,
~10 V pulled up, unconnected in car) - possible service-mode input that could
permit bench contactor operation. Function unproven; needs Tesla service docs
or careful testing.
