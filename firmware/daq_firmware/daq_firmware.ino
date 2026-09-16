/* ============================================================================
 *  Cold plate loop DAQ firmware
 *
 *  Streams one line per sample over USB serial:
 *
 *      D,<seq>,<millis>,<v1>,<v2>,...,<vN>*<xor>
 *
 *  The values are in the same order as `channels:` in config.yaml. That order
 *  is the contract between this sketch and the PC - if you add, remove or
 *  reorder a channel, change it in BOTH places.
 *
 *  On boot (and whenever the PC sends '?') it also sends a header line:
 *
 *      #DAQ1 {"fw":"1.0.0","rate_hz":10,"channels":[...]}
 *
 *  which the server checks against config.yaml and warns about on mismatch.
 *
 *  ---------------------------------------------------------------------------
 *  BEFORE YOU RUN THIS
 *  ---------------------------------------------------------------------------
 *  Every sensor block below is marked CONFIGURE. Set the pins and constants to
 *  match how you actually wire the rig. Until then the sketch compiles and
 *  streams, but the numbers are only as good as the constants here.
 *
 *  Pin budget note: an Uno has six analog inputs (A0-A5), which this sketch
 *  spends on four temperatures and two pressures. Heater power is therefore a
 *  value you set from the PC (send "W,420" over serial) rather than a measured
 *  channel. On a Mega or an ESP32 there are spare ADC pins - see HEATER below.
 * ========================================================================== */

#include <Arduino.h>
#if defined(ARDUINO_ARCH_SAMD)
#include <avr/dtostrf.h>
#endif

// ---------------------------------------------------------------- transport

static const uint32_t BAUD_RATE = 115200;
static const uint16_t SAMPLE_RATE_HZ = 10;
static const char FIRMWARE_VERSION[] = "1.0.0";

// Channel order. MUST match `channels:` in config.yaml.
static const char CHANNEL_IDS[] =
    "\"T_in\",\"T_out\",\"T_case\",\"T_amb\",\"P_in\",\"P_out\",\"Q_flow\",\"W_heater\"";
static const uint8_t CHANNEL_COUNT = 8;

// --------------------------------------------------------- CONFIGURE: ADC

// Uno/Nano/Mega read 0-1023 against 5 V. An ESP32 reads 0-4095 against 3.3 V.
static const float ADC_MAX = 1023.0f;
static const float ADC_VREF = 5.0f;
// Each reading is the mean of this many samples, to trade rate for noise.
static const uint8_t OVERSAMPLE = 8;

// -------------------------------------------- CONFIGURE: temperature inputs

// Four 10k NTC thermistors, each the LOWER leg of a divider against a 10k
// fixed resistor to VCC. Swap THERMISTOR_ON_GROUND to false if you wired the
// thermistor to VCC instead.
static const uint8_t TEMP_PINS[4] = {A0, A1, A2, A3};
static const float THERM_SERIES_OHMS = 10000.0f;
static const bool THERMISTOR_ON_GROUND = true;

// Steinhart-Hart coefficients. These are for a generic 10k/B3950 NTC; replace
// them with the values from your thermistor's datasheet, or fit your own from
// a three-point ice/room/hot calibration.
static const float SH_A = 1.009249522e-03f;
static const float SH_B = 2.378405444e-04f;
static const float SH_C = 2.019202697e-07f;

// ----------------------------------------------- CONFIGURE: pressure inputs

// Ratiometric 0.5-4.5 V transducers. For a 4-20 mA sensor across a 165 ohm
// sense resistor the input span becomes 0.66-3.30 V - change V_MIN/V_MAX.
static const uint8_t PRESSURE_PINS[2] = {A4, A5};
static const float PRESSURE_V_MIN = 0.5f;
static const float PRESSURE_V_MAX = 4.5f;
static const float PRESSURE_KPA_MIN = 0.0f;
static const float PRESSURE_KPA_MAX = 690.0f;  // 100 psi full scale

// --------------------------------------------------- CONFIGURE: flow meter
//
// Pick ONE flow mode.
//   FLOW_MODE_PULSE   ultrasonic/turbine meter with a frequency output
//   FLOW_MODE_ANALOG  meter with a 0-5 V or 4-20 mA output
//   FLOW_MODE_NONE    no meter yet; reports 0
#define FLOW_MODE_PULSE

// Pulse mode: the pin must support interrupts (D2 or D3 on an Uno).
static const uint8_t FLOW_PULSE_PIN = 2;
// Pulses per litre, from the meter's datasheet. A common inline sensor is 450.
static const float FLOW_K_PULSES_PER_LITRE = 450.0f;

// Analog mode: pin and the flow the endpoints correspond to.
static const uint8_t FLOW_ANALOG_PIN = A6;
static const float FLOW_V_MIN = 0.5f;
static const float FLOW_V_MAX = 4.5f;
static const float FLOW_LPM_MIN = 0.0f;
static const float FLOW_LPM_MAX = 10.0f;

// ------------------------------------------------------- CONFIGURE: heater
//
// Define HEATER_ANALOG_PIN to measure heater power instead of setting it from
// the PC. Leave it undefined on an Uno - there are no analog pins left.
// #define HEATER_ANALOG_PIN A7
static const float HEATER_W_MIN = 0.0f;
static const float HEATER_W_MAX = 1000.0f;

// ============================================================================

static volatile uint32_t flowPulses = 0;
static uint32_t lastFlowSampleMs = 0;
static float heaterWatts = 0.0f;
static uint32_t sequence = 0;
static uint32_t nextSampleMs = 0;

#if defined(FLOW_MODE_PULSE)
static void onFlowPulse() { flowPulses++; }
#endif

static float readVolts(uint8_t pin) {
  uint32_t total = 0;
  for (uint8_t i = 0; i < OVERSAMPLE; i++) {
    total += analogRead(pin);
  }
  return (total / (float)OVERSAMPLE) * ADC_VREF / ADC_MAX;
}

static float rescale(float value, float inMin, float inMax, float outMin, float outMax) {
  if (inMax == inMin) return NAN;
  return outMin + (value - inMin) * (outMax - outMin) / (inMax - inMin);
}

static float readThermistorC(uint8_t pin) {
  float volts = readVolts(pin);
  if (volts <= 0.001f || volts >= ADC_VREF - 0.001f) return NAN;  // open or shorted

  float resistance = THERMISTOR_ON_GROUND
                         ? THERM_SERIES_OHMS * volts / (ADC_VREF - volts)
                         : THERM_SERIES_OHMS * (ADC_VREF - volts) / volts;
  if (resistance <= 0.0f) return NAN;

  float lnR = log(resistance);
  float invT = SH_A + SH_B * lnR + SH_C * lnR * lnR * lnR;
  if (invT == 0.0f) return NAN;
  return 1.0f / invT - 273.15f;
}

static float readPressureKpa(uint8_t pin) {
  return rescale(readVolts(pin), PRESSURE_V_MIN, PRESSURE_V_MAX,
                 PRESSURE_KPA_MIN, PRESSURE_KPA_MAX);
}

static float readFlowLpm() {
#if defined(FLOW_MODE_PULSE)
  uint32_t now = millis();
  uint32_t elapsed = now - lastFlowSampleMs;
  if (elapsed == 0) return NAN;

  noInterrupts();
  uint32_t pulses = flowPulses;
  flowPulses = 0;
  interrupts();

  lastFlowSampleMs = now;
  float hz = pulses * 1000.0f / elapsed;
  return hz * 60.0f / FLOW_K_PULSES_PER_LITRE;

#elif defined(FLOW_MODE_ANALOG)
  return rescale(readVolts(FLOW_ANALOG_PIN), FLOW_V_MIN, FLOW_V_MAX,
                 FLOW_LPM_MIN, FLOW_LPM_MAX);
#else
  return 0.0f;
#endif
}

static float readHeaterWatts() {
#if defined(HEATER_ANALOG_PIN)
  return rescale(readVolts(HEATER_ANALOG_PIN), 0.0f, ADC_VREF, HEATER_W_MIN, HEATER_W_MAX);
#else
  return heaterWatts;  // set from the PC with "W,<watts>"
#endif
}

static char *formatFloat(char *dst, size_t n, float value) {
  if (isnan(value) || isinf(value)) {
    strncpy(dst, "nan", n);
    dst[n - 1] = '\0';
    return dst;
  }
#if defined(__AVR__) || defined(ARDUINO_ARCH_SAMD)
  dtostrf(value, 0, 3, dst);
#else
  snprintf(dst, n, "%.3f", value);
#endif
  return dst;
}

static void sendHeader() {
  Serial.print(F("#DAQ1 {\"fw\":\""));
  Serial.print(FIRMWARE_VERSION);
  Serial.print(F("\",\"rate_hz\":"));
  Serial.print(SAMPLE_RATE_HZ);
  Serial.print(F(",\"channels\":["));
  Serial.print(CHANNEL_IDS);
  Serial.println(F("]}"));
}

static void sendSample(const float *values) {
  char line[192];
  char number[16];

  int written = snprintf(line, sizeof(line), "D,%lu,%lu",
                         (unsigned long)sequence, (unsigned long)millis());

  for (uint8_t i = 0; i < CHANNEL_COUNT && written > 0 && written < (int)sizeof(line); i++) {
    formatFloat(number, sizeof(number), values[i]);
    written += snprintf(line + written, sizeof(line) - written, ",%s", number);
  }

  uint8_t checksum = 0;
  for (int i = 0; line[i] != '\0'; i++) checksum ^= (uint8_t)line[i];

  Serial.print(line);
  Serial.print('*');
  if (checksum < 0x10) Serial.print('0');
  Serial.println(checksum, HEX);
}

// Accepts "?" to resend the header and "W,<watts>" to set the heater setpoint.
static void handleCommands() {
  static char buffer[32];
  static uint8_t length = 0;

  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\n' || c == '\r') {
      buffer[length] = '\0';
      if (length > 0) {
        if (buffer[0] == '?') {
          sendHeader();
        } else if (buffer[0] == 'W' && buffer[1] == ',') {
          heaterWatts = atof(buffer + 2);
          Serial.print(F("!info heater setpoint "));
          Serial.println(heaterWatts);
        }
      }
      length = 0;
    } else if (length < sizeof(buffer) - 1) {
      buffer[length++] = c;
    }
  }
}

void setup() {
  Serial.begin(BAUD_RATE);
  while (!Serial && millis() < 3000) {
    ;  // native-USB boards need a moment; don't hang a board without a host
  }

  for (uint8_t i = 0; i < 4; i++) pinMode(TEMP_PINS[i], INPUT);
  for (uint8_t i = 0; i < 2; i++) pinMode(PRESSURE_PINS[i], INPUT);

#if defined(FLOW_MODE_PULSE)
  pinMode(FLOW_PULSE_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(FLOW_PULSE_PIN), onFlowPulse, FALLING);
  lastFlowSampleMs = millis();
#endif

  sendHeader();
  nextSampleMs = millis();
}

void loop() {
  handleCommands();

  uint32_t now = millis();
  if ((int32_t)(now - nextSampleMs) < 0) return;
  nextSampleMs += 1000UL / SAMPLE_RATE_HZ;
  if ((int32_t)(now - nextSampleMs) > 0) nextSampleMs = now;  // fell behind; resync

  float values[CHANNEL_COUNT];
  values[0] = readThermistorC(TEMP_PINS[0]);   // T_in
  values[1] = readThermistorC(TEMP_PINS[1]);   // T_out
  values[2] = readThermistorC(TEMP_PINS[2]);   // T_case
  values[3] = readThermistorC(TEMP_PINS[3]);   // T_amb
  values[4] = readPressureKpa(PRESSURE_PINS[0]);  // P_in
  values[5] = readPressureKpa(PRESSURE_PINS[1]);  // P_out
  values[6] = readFlowLpm();                   // Q_flow
  values[7] = readHeaterWatts();               // W_heater

  sequence++;
  sendSample(values);
}
