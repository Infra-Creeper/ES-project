#include <WiFi.h>
#include <WiFiClient.h>
#include <LittleFS.h>
#include <driver/i2s.h>
#include <time.h>

// ---------------- Wi-Fi settings ----------------
const char* WIFI_SSID = "Lenovo IdeaPad";
const char* WIFI_PASSWORD = "cyclonejoker";

// Use your computer's LAN IPv4 address.
// Do NOT use localhost or 127.0.0.1 here.
const char* SERVER_HOST = "192.168.137.1";
const uint16_t SERVER_PORT = 8000;
const char* SERVER_PATH = "/api/detections/analyze";

// ---------------- API fields ----------------
const char* STATION_ID = "ST-001";

// ---------------- Audio settings ----------------
#define I2S_PORT       I2S_NUM_0
#define I2S_BCLK       12
#define I2S_WS         13
#define I2S_DIN        14

const uint32_t SAMPLE_RATE = 32000;
const uint32_t RECORD_SECONDS = 5;
const uint32_t TOTAL_SAMPLES =
    SAMPLE_RATE * RECORD_SECONDS;

const char* WAV_PATH = "/recording.wav";

const char* BOUNDARY = "----ESP32Boundary7MA4YWxk";

// ---------------- WAV helpers ----------------
void writeLE16(uint8_t* p, uint16_t value) {
  p[0] = value & 0xFF;
  p[1] = (value >> 8) & 0xFF;
}

void writeLE32(uint8_t* p, uint32_t value) {
  p[0] = value & 0xFF;
  p[1] = (value >> 8) & 0xFF;
  p[2] = (value >> 16) & 0xFF;
  p[3] = (value >> 24) & 0xFF;
}

void makeWavHeader(uint8_t* h, uint32_t dataSize) {
  memcpy(h + 0, "RIFF", 4);
  writeLE32(h + 4, 36 + dataSize);
  memcpy(h + 8, "WAVE", 4);

  memcpy(h + 12, "fmt ", 4);
  writeLE32(h + 16, 16);              // PCM format chunk
  writeLE16(h + 20, 1);               // PCM
  writeLE16(h + 22, 1);               // Mono
  writeLE32(h + 24, SAMPLE_RATE);
  writeLE32(h + 28, SAMPLE_RATE * 2); // Bytes per second
  writeLE16(h + 32, 2);               // Block alignment
  writeLE16(h + 34, 16);              // Bits per sample

  memcpy(h + 36, "data", 4);
  writeLE32(h + 40, dataSize);
}

// ---------------- Wi-Fi ----------------
bool connectWiFi() {
  Serial.printf("Connecting to Wi-Fi: %s\n", WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();

  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");

    if (millis() - start > 30000) {
      Serial.println("\nWi-Fi connection failed.");
      return false;
    }
  }

  Serial.println("\nWi-Fi connected.");
  Serial.print("ESP32 IP: ");
  Serial.println(WiFi.localIP());

  return true;
}

// ---------------- UTC timestamp ----------------
bool getUtcTimestamp(char* output, size_t outputSize) {
  // UTC timezone: no local timezone offset.
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");

  Serial.println("Synchronizing UTC time...");

  struct tm timeInfo;

  if (!getLocalTime(&timeInfo, 15000)) {
    Serial.println("Could not obtain UTC time.");
    return false;
  }

  strftime(output, outputSize,
           "%Y-%m-%dT%H:%M:%SZ", &timeInfo);

  Serial.printf("Recorded at: %s\n", output);
  return true;
}

// ---------------- I2S setup ----------------
bool setupMicrophone() {
  i2s_config_t config = {};

  config.mode = (i2s_mode_t)(
      I2S_MODE_MASTER | I2S_MODE_RX
  );

  config.sample_rate = SAMPLE_RATE;
  config.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  config.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  config.dma_buf_count = 8;
  config.dma_buf_len = 512;
  config.use_apll = false;
  config.tx_desc_auto_clear = false;
  config.fixed_mclk = 0;

  esp_err_t result = i2s_driver_install(
      I2S_PORT, &config, 0, nullptr
  );

  if (result != ESP_OK) {
    Serial.printf("I2S driver install failed: %d\n", result);
    return false;
  }

  i2s_pin_config_t pins = {};

  pins.bck_io_num = I2S_BCLK;
  pins.ws_io_num = I2S_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = I2S_DIN;

  result = i2s_set_pin(I2S_PORT, &pins);

  if (result != ESP_OK) {
    Serial.printf("I2S pin setup failed: %d\n", result);
    i2s_driver_uninstall(I2S_PORT);
    return false;
  }

  // Discard initial samples while the microphone starts.
  int32_t discard[128];
  size_t bytesRead = 0;

  i2s_read(
      I2S_PORT,
      discard,
      sizeof(discard),
      &bytesRead,
      pdMS_TO_TICKS(100)
  );

  Serial.println("INMP441 I2S initialized.");
  return true;
}

// ---------------- Record WAV ----------------
bool recordWav() {
  Serial.println("\nStarting 5-second recording...");

  LittleFS.remove(WAV_PATH);

  File file = LittleFS.open(WAV_PATH, FILE_WRITE);

  if (!file) {
    Serial.println("Could not create WAV file.");
    return false;
  }

  // Reserve the first 44 bytes for the WAV header.
  uint8_t emptyHeader[44] = {};
  if (file.write(emptyHeader, sizeof(emptyHeader))
      != sizeof(emptyHeader)) {
    Serial.println("Could not write WAV header.");
    file.close();
    return false;
  }

  int32_t rawBuffer[512];
  int16_t pcmBuffer[512];

  uint32_t samplesWritten = 0;
  int consecutiveEmptyReads = 0;

  while (samplesWritten < TOTAL_SAMPLES) {
    size_t bytesRead = 0;

    esp_err_t result = i2s_read(
        I2S_PORT,
        rawBuffer,
        sizeof(rawBuffer),
        &bytesRead,
        pdMS_TO_TICKS(1000)
    );

    if (result != ESP_OK || bytesRead == 0) {
      consecutiveEmptyReads++;

      if (consecutiveEmptyReads >= 10) {
        Serial.println("No microphone data received.");
        file.close();
        return false;
      }

      continue;
    }

    consecutiveEmptyReads = 0;

    uint32_t availableSamples =
        bytesRead / sizeof(int32_t);

    uint32_t remainingSamples =
        TOTAL_SAMPLES - samplesWritten;

    uint32_t count = availableSamples;

    if (count > remainingSamples) {
      count = remainingSamples;
    }

    // INMP441 supplies 24-bit audio in a 32-bit I2S slot.
    // Shift to obtain signed 16-bit PCM samples.
    for (uint32_t i = 0; i < count; i++) {
      pcmBuffer[i] = (int16_t)(rawBuffer[i] >> 14);
    }

    size_t bytesToWrite = count * sizeof(int16_t);

    if (file.write(
          (const uint8_t*)pcmBuffer,
          bytesToWrite
        ) != bytesToWrite) {
      Serial.println("Failed to write audio data.");
      file.close();
      return false;
    }

    samplesWritten += count;

    if (samplesWritten % SAMPLE_RATE < count) {
      Serial.printf(
          "Recorded %lu / %lu samples\n",
          (unsigned long)samplesWritten,
          (unsigned long)TOTAL_SAMPLES
      );
    }
  }

  // Rewrite the WAV header with the final data size.
  uint32_t audioBytes = TOTAL_SAMPLES * 2;
  uint8_t header[44];

  makeWavHeader(header, audioBytes);

  if (!file.seek(0) ||
      file.write(header, sizeof(header)) != sizeof(header)) {
    Serial.println("Failed to finalize WAV header.");
    file.close();
    return false;
  }

  file.flush();
  file.close();

  File check = LittleFS.open(WAV_PATH, FILE_READ);

  if (!check) {
    Serial.println("Could not reopen WAV file.");
    return false;
  }

  Serial.printf(
      "Recording complete. WAV size: %lu bytes\n",
      (unsigned long)check.size()
  );

  check.close();
  return true;
}

// ---------------- Reliable socket write ----------------
bool writeAll(
    WiFiClient& client,
    const uint8_t* data,
    size_t length
) {
  size_t sent = 0;
  unsigned long start = millis();

  while (sent < length) {
    if (!client.connected() && !client.available()) {
      return false;
    }

    size_t written = client.write(data + sent, length - sent);

    if (written > 0) {
      sent += written;
      start = millis();
    } else {
      if (millis() - start > 15000) {
        return false;
      }
      delay(1);
    }
  }

  return true;
}

// ---------------- Multipart HTTP upload ----------------
bool uploadWav(const char* recordedAt) {
  File file = LittleFS.open(WAV_PATH, FILE_READ);

  if (!file) {
    Serial.println("Could not open WAV for upload.");
    return false;
  }

  const String fileHeader =
      String("--") + BOUNDARY + "\r\n"
      "Content-Disposition: form-data; "
      "name=\"file\"; filename=\"recording.wav\"\r\n"
      "Content-Type: audio/wav\r\n\r\n";

  const String stationPart =
      String("\r\n--") + BOUNDARY + "\r\n"
      "Content-Disposition: form-data; name=\"station_id\"\r\n\r\n"
      + STATION_ID + "\r\n";

  const String timePart =
      String("--") + BOUNDARY + "\r\n"
      "Content-Disposition: form-data; name=\"recorded_at\"\r\n\r\n"
      + recordedAt + "\r\n";

  const String closing =
      String("--") + BOUNDARY + "--\r\n";

  uint32_t fileSize = file.size();

  uint32_t contentLength =
      fileHeader.length() +
      fileSize +
      stationPart.length() +
      timePart.length() +
      closing.length();

  WiFiClient client;
  client.setTimeout(15000);

  Serial.printf(
      "\nConnecting to %s:%u...\n",
      SERVER_HOST, SERVER_PORT
  );

  if (!client.connect(SERVER_HOST, SERVER_PORT)) {
    Serial.println("Could not connect to the API server.");
    file.close();
    return false;
  }

  client.printf("POST %s HTTP/1.1\r\n", SERVER_PATH);
  client.printf("Host: %s:%u\r\n", SERVER_HOST, SERVER_PORT);
  client.println(
      "Content-Type: multipart/form-data; boundary=----ESP32Boundary7MA4YWxk"
  );
  client.printf("Content-Length: %lu\r\n",
                (unsigned long)contentLength);
  client.println("Connection: close");
  client.println();

  bool ok = true;

  // Part 1: WAV file.
  ok = writeAll(
      client,
      (const uint8_t*)fileHeader.c_str(),
      fileHeader.length()
  );

  // Stream the file in small blocks.
  uint8_t buffer[2048];

  while (ok && file.available()) {
    size_t n = file.read(buffer, sizeof(buffer));

    if (n == 0) {
      break;
    }

    ok = writeAll(client, buffer, n);
  }

  file.close();

  // Part 2: station_id.
  if (ok) {
    ok = writeAll(
        client,
        (const uint8_t*)stationPart.c_str(),
        stationPart.length()
    );
  }

  // Part 3: recorded_at.
  if (ok) {
    ok = writeAll(
        client,
        (const uint8_t*)timePart.c_str(),
        timePart.length()
    );
  }

  // End multipart body.
  if (ok) {
    ok = writeAll(
        client,
        (const uint8_t*)closing.c_str(),
        closing.length()
    );
  }

  if (!ok) {
    Serial.println("Failed while sending HTTP request.");
    client.stop();
    return false;
  }

  Serial.println("Upload sent. Waiting for API response...");

  unsigned long start = millis();

  while (!client.available() &&
         client.connected() &&
         millis() - start < 20000) {
    delay(10);
  }

  if (!client.available()) {
    Serial.println("No HTTP response received.");
    client.stop();
    return false;
  }

  String statusLine = client.readStringUntil('\n');
  statusLine.trim();

  Serial.print("HTTP response: ");
  Serial.println(statusLine);

  int firstSpace = statusLine.indexOf(' ');
  int statusCode = -1;

  if (firstSpace >= 0) {
    statusCode = statusLine.substring(firstSpace + 1).toInt();
  }

  // Print the response body for FastAPI errors or success details.
  unsigned long responseStart = millis();

  while ((client.connected() || client.available()) &&
         millis() - responseStart < 5000) {
    while (client.available()) {
      Serial.write(client.read());
      responseStart = millis();
    }
    delay(1);
  }

  client.stop();

  if (statusCode >= 200 && statusCode < 300) {
    Serial.println("\nUpload successful.");
    return true;
  }

  Serial.printf("\nUpload failed. HTTP status: %d\n", statusCode);
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("ESP32 + INMP441 Continuous Audio Recorder");

  if (!LittleFS.begin(true)) {
    Serial.println("LittleFS initialization failed.");
    while (true) {
      delay(1000);
    }
  }

  while (!connectWiFi()) {
    Serial.println("Retrying Wi-Fi in 5 seconds...");
    delay(5000);
  }

  if (!setupMicrophone()) {
    Serial.println("Microphone initialization failed.");
    while (true) {
      delay(1000);
    }
  }

  Serial.println("Setup complete. Starting continuous recording.");
}

// ---------------- Continuous recording loop ----------------
void loop() {
  // Reconnect if Wi-Fi disconnects.
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi disconnected. Reconnecting...");

    while (!connectWiFi()) {
      delay(5000);
    }
  }

  Serial.println("\n================================");
  Serial.println("Starting next recording cycle");
  Serial.println("================================");

  char recordedAt[32];

  // Get the UTC timestamp before recording.
  if (!getUtcTimestamp(recordedAt, sizeof(recordedAt))) {
    Serial.println("Time unavailable. Skipping this cycle.");
    delay(5000);
    return;
  }

  // Record exactly 5 seconds of audio.
  if (!recordWav()) {
    Serial.println("Recording failed. Retrying in 3 seconds.");
    delay(3000);
    return;
  }

  // Upload the WAV file to FastAPI.
  if (uploadWav(recordedAt)) {
    Serial.println("Recording uploaded successfully.");
  } else {
    Serial.println("Upload failed. Continuing with next cycle.");
  }

  // Pause between recording cycles.
  Serial.println("Next recording starts in 1 seconds.");
  delay(1000);
}