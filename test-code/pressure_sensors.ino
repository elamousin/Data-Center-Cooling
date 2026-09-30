// For 0.5-4.5 V ratiometric sensors (Either Honeywell PX3, JCI P499)
// V_out = 0.5 V at 0 psi, 4.5 V at full scale
float readRatiometric(int pin, float p_full) {
  int raw = analogRead(pin);
  float v_out = raw * (V_SUPPLY / 1023.0);
  float pressure = ((v_out / V_SUPPLY) - 0.1) * (p_full / 0.8);
  return pressure;
}

// For 4-20 mA loop sensors (Dwyer 629C, Kele DPW)
// I = 4 mA at 0 psi, 20 mA at full scale
// V_read = I x R_SENSE
float readCurrentLoop(int pin, float p_full) {
  int raw = analogRead(pin);
  float v_read = raw * (V_SUPPLY / 1023.0);
  float current_mA = (v_read / R_SENSE) * 1000.0;
  float pressure = ((current_mA - 4.0) / 16.0) * p_full;
  return pressure;
}
