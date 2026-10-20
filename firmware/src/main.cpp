// Caméra de ligne d'arrivée — firmware ESP32-S3
//
// Rôle volontairement minimal : capturer, horodater, diffuser.
// Toute l'analyse (détection des franchissements, classement) tourne sur le Raspberry Pi.
//
// Points d'accès HTTP (tous protégés par « Authorization: Bearer <CAMERA_TOKEN> ») :
//   port 80  GET /status   -> JSON d'état (heure synchronisée, fps, RSSI, mémoire)
//            GET /capture  -> une image JPEG (en-tête X-Timestamp)
//            GET /control?var=<nom>&val=<int> -> réglage capteur
//   port 81  GET /stream   -> flux MJPEG multipart, chaque image porte X-Timestamp
//
// X-Timestamp = instant de capture en microsecondes depuis l'epoch Unix (UTC),
// synchronisé par NTP sur le Pi. C'est cet horodatage qui sert au chronométrage,
// pas l'heure de réception côté Pi (qui inclut la latence Wi-Fi).

#include <Arduino.h>
#include <WiFi.h>
#include <esp_camera.h>
#include <esp_http_server.h>
#include <esp_sntp.h>
#include <esp_timer.h>
#include <sys/time.h>

#include <atomic>

#include "camera_pins.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "secrets.h absent : utilisation de secrets.example.h (NE PAS déployer ainsi)"
#include "secrets.example.h"
#endif

static httpd_handle_t ctrl_httpd = nullptr;
static httpd_handle_t stream_httpd = nullptr;

static volatile bool g_time_synced = false;
static volatile float g_stream_fps = 0.0f;
static std::atomic<uint32_t> g_stream_clients{0};

static constexpr char BOUNDARY[] = "frame";
static constexpr char STREAM_CONTENT_TYPE[] = "multipart/x-mixed-replace;boundary=frame";

// ---------------------------------------------------------------------------
// Temps
// ---------------------------------------------------------------------------

static void onTimeSync(struct timeval *) { g_time_synced = true; }

static int64_t epochMicros() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (int64_t)tv.tv_sec * 1000000LL + tv.tv_usec;
}

// fb->timestamp est relevé avec esp_timer (µs depuis le démarrage).
// On le convertit en temps Unix grâce au décalage courant horloge système / esp_timer.
static int64_t frameEpochMicros(const camera_fb_t *fb) {
  int64_t frame_boot_us = (int64_t)fb->timestamp.tv_sec * 1000000LL + fb->timestamp.tv_usec;
  int64_t offset = epochMicros() - esp_timer_get_time();
  return frame_boot_us + offset;
}

// ---------------------------------------------------------------------------
// Authentification
// ---------------------------------------------------------------------------

// Comparaison en temps constant : évite de révéler le jeton par mesure de temps de réponse.
static bool constantTimeEquals(const char *a, const char *b) {
  size_t la = strlen(a), lb = strlen(b);
  uint8_t diff = (uint8_t)(la ^ lb);
  for (size_t i = 0; i < la; i++) diff |= (uint8_t)(a[i] ^ b[i % (lb ? lb : 1)]);
  return diff == 0;
}

static bool authorized(httpd_req_t *req) {
  char value[128];
  if (httpd_req_get_hdr_value_str(req, "Authorization", value, sizeof(value)) != ESP_OK) {
    return false;
  }
  static const char prefix[] = "Bearer ";
  if (strncmp(value, prefix, sizeof(prefix) - 1) != 0) return false;
  return constantTimeEquals(value + sizeof(prefix) - 1, CAMERA_TOKEN);
}

static esp_err_t deny(httpd_req_t *req) {
  httpd_resp_set_status(req, "401 Unauthorized");
  httpd_resp_set_hdr(req, "WWW-Authenticate", "Bearer");
  return httpd_resp_send(req, "unauthorized", HTTPD_RESP_USE_STRLEN);
}

// ---------------------------------------------------------------------------
// Gestionnaires HTTP
// ---------------------------------------------------------------------------

static esp_err_t statusHandler(httpd_req_t *req) {
  if (!authorized(req)) return deny(req);
  sensor_t *s = esp_camera_sensor_get();
  char json[384];
  snprintf(json, sizeof(json),
           "{\"time_synced\":%s,\"epoch_us\":%lld,\"uptime_s\":%llu,"
           "\"stream_fps\":%.1f,\"stream_clients\":%lu,\"rssi\":%d,"
           "\"heap_free\":%lu,\"psram_free\":%lu,\"sensor_pid\":%u,"
           "\"framesize\":%d,\"quality\":%d}",
           g_time_synced ? "true" : "false", (long long)epochMicros(),
           (unsigned long long)(esp_timer_get_time() / 1000000ULL), g_stream_fps,
           (unsigned long)g_stream_clients.load(), WiFi.RSSI(), (unsigned long)ESP.getFreeHeap(),
           (unsigned long)ESP.getFreePsram(), s ? s->id.PID : 0,
           s ? (int)s->status.framesize : -1, s ? s->status.quality : -1);
  httpd_resp_set_type(req, "application/json");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  return httpd_resp_send(req, json, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t captureHandler(httpd_req_t *req) {
  if (!authorized(req)) return deny(req);
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  char ts[24];
  snprintf(ts, sizeof(ts), "%lld", (long long)frameEpochMicros(fb));
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "X-Timestamp", ts);
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);
  return res;
}

// Réglages capteur utiles sur le terrain (exposition, contre-jour…).
// Liste blanche explicite : aucune autre variable n'est modifiable.
static esp_err_t controlHandler(httpd_req_t *req) {
  if (!authorized(req)) return deny(req);
  char query[96], var[24], val[12];
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK ||
      httpd_query_key_value(query, "var", var, sizeof(var)) != ESP_OK ||
      httpd_query_key_value(query, "val", val, sizeof(val)) != ESP_OK) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "var et val requis");
    return ESP_FAIL;
  }
  int v = atoi(val);
  sensor_t *s = esp_camera_sensor_get();
  int r = -1;
  if (!strcmp(var, "framesize") && v >= FRAMESIZE_QVGA && v <= FRAMESIZE_SXGA) r = s->set_framesize(s, (framesize_t)v);
  else if (!strcmp(var, "quality") && v >= 6 && v <= 40) r = s->set_quality(s, v);
  else if (!strcmp(var, "brightness") && v >= -2 && v <= 2) r = s->set_brightness(s, v);
  else if (!strcmp(var, "contrast") && v >= -2 && v <= 2) r = s->set_contrast(s, v);
  else if (!strcmp(var, "ae_level") && v >= -2 && v <= 2) r = s->set_ae_level(s, v);
  else if (!strcmp(var, "aec") && (v == 0 || v == 1)) r = s->set_exposure_ctrl(s, v);
  else if (!strcmp(var, "aec_value") && v >= 0 && v <= 1200) r = s->set_aec_value(s, v);
  else if (!strcmp(var, "hmirror") && (v == 0 || v == 1)) r = s->set_hmirror(s, v);
  else if (!strcmp(var, "vflip") && (v == 0 || v == 1)) r = s->set_vflip(s, v);
  if (r != 0) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "variable ou valeur refusee");
    return ESP_FAIL;
  }
  return httpd_resp_send(req, "ok", HTTPD_RESP_USE_STRLEN);
}

static esp_err_t streamHandler(httpd_req_t *req) {
  if (!authorized(req)) return deny(req);

  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  g_stream_clients++;

  char part[160];
  int64_t last_us = esp_timer_get_time();
  float fps = 0.0f;

  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
      log_e("Echec de capture");
      res = ESP_FAIL;
      break;
    }
    int hlen = snprintf(part, sizeof(part),
                        "\r\n--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n"
                        "X-Timestamp: %lld\r\n\r\n",
                        BOUNDARY, (unsigned)fb->len, (long long)frameEpochMicros(fb));
    res = httpd_resp_send_chunk(req, part, hlen);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    if (res != ESP_OK) break;  // client déconnecté

    int64_t now = esp_timer_get_time();
    float inst = 1e6f / (float)(now - last_us);
    fps = fps == 0.0f ? inst : fps * 0.9f + inst * 0.1f;  // moyenne glissante
    g_stream_fps = fps;
    last_us = now;
  }
  g_stream_clients--;
  if (g_stream_clients == 0) g_stream_fps = 0.0f;
  return res;
}

static void startServers() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.max_uri_handlers = 8;

  httpd_uri_t status_uri = {.uri = "/status", .method = HTTP_GET, .handler = statusHandler, .user_ctx = nullptr};
  httpd_uri_t capture_uri = {.uri = "/capture", .method = HTTP_GET, .handler = captureHandler, .user_ctx = nullptr};
  httpd_uri_t control_uri = {.uri = "/control", .method = HTTP_GET, .handler = controlHandler, .user_ctx = nullptr};
  if (httpd_start(&ctrl_httpd, &cfg) == ESP_OK) {
    httpd_register_uri_handler(ctrl_httpd, &status_uri);
    httpd_register_uri_handler(ctrl_httpd, &capture_uri);
    httpd_register_uri_handler(ctrl_httpd, &control_uri);
  }

  // Le flux bloque sa tâche serveur : il tourne sur un serveur dédié (port 81).
  cfg.server_port = 81;
  cfg.ctrl_port += 1;
  httpd_uri_t stream_uri = {.uri = "/stream", .method = HTTP_GET, .handler = streamHandler, .user_ctx = nullptr};
  if (httpd_start(&stream_httpd, &cfg) == ESP_OK) {
    httpd_register_uri_handler(stream_httpd, &stream_uri);
  }
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------

static bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;
  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;
  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;
  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAMESIZE_VGA;  // 640x480 : ~1,6 cm/pixel sur 5 m de large à 320 px de bande
  c.jpeg_quality = 12;           // 10-15 : bon compromis netteté des dossards / débit Wi-Fi
  c.fb_count = 2;
  c.fb_location = CAMERA_FB_IN_PSRAM;
  c.grab_mode = CAMERA_GRAB_LATEST;  // toujours la dernière image : pas de retard accumulé

  if (!psramFound()) {
    log_e("PSRAM introuvable : verifier board_build.arduino.memory_type");
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.frame_size = FRAMESIZE_QVGA;
  }

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    log_e("esp_camera_init a echoue : 0x%x", err);
    return false;
  }

  sensor_t *s = esp_camera_sensor_get();
  log_i("Capteur detecte : PID 0x%x", s->id.PID);
  if (s->id.PID == OV3660_PID) {
    s->set_vflip(s, 1);  // l'OV3660 est monté retourné sur la plupart des cartes S3
    s->set_brightness(s, 1);
    s->set_saturation(s, -2);
  }
  return true;
}

static void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // latence minimale ; la carte est alimentée sur secteur
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Connexion a %s", WIFI_SSID);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print('.');
    if (millis() - t0 > 30000) {
      Serial.println("\nWi-Fi indisponible, redemarrage");
      ESP.restart();
    }
  }
  Serial.printf("\nConnecte, IP %s\n", WiFi.localIP().toString().c_str());
}

void setup() {
  Serial.begin(115200);
  Serial.println("\n== Camera ligne d'arrivee ==");

  if (!initCamera()) {
    delay(5000);
    ESP.restart();
  }
  connectWifi();

  sntp_set_time_sync_notification_cb(onTimeSync);
  sntp_set_sync_interval(60 * 1000);  // resynchronisation chaque minute : dérive négligeable
  configTime(0, 0, NTP_SERVER);       // UTC ; l'affichage local est géré par le Pi

  startServers();
  Serial.println("Serveurs demarres : :80/status /capture /control, :81/stream");
}

void loop() {
  // Chien de garde réseau : sans Wi-Fi pendant 60 s, on redémarre proprement.
  static uint32_t lost_since = 0;
  if (WiFi.status() != WL_CONNECTED) {
    if (!lost_since) lost_since = millis();
    if (millis() - lost_since > 60000) ESP.restart();
  } else {
    lost_since = 0;
  }
  delay(1000);
}
