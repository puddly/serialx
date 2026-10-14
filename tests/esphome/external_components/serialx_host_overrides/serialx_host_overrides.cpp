#ifdef USE_HOST

#include "serialx_host_overrides.h"

#include "esphome/components/api/api_server.h"
#include "esphome/core/alloc_helpers.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#include <cerrno>
#include <cstdlib>

namespace esphome::serialx_host_overrides {

static const char *const TAG = "serialx.host_overrides";

void SerialxHostOverridesComponent::setup() {
  if (this->left_uart_ == nullptr || this->right_uart_ == nullptr) {
    ESP_LOGE(TAG, "UART targets are not configured");
    this->mark_failed();
    return;
  }

  if (const char *left_port = std::getenv(this->left_uart_env_.c_str());
      left_port != nullptr && left_port[0] != '\0') {
    this->left_uart_->set_name(left_port);
    ESP_LOGI(TAG, "Overrode left UART path from %s", this->left_uart_env_.c_str());
  }

  if (const char *right_port = std::getenv(this->right_uart_env_.c_str());
      right_port != nullptr && right_port[0] != '\0') {
    this->right_uart_->set_name(right_port);
    ESP_LOGI(TAG, "Overrode right UART path from %s", this->right_uart_env_.c_str());
  }

  const char *api_port_value = std::getenv(this->api_port_env_.c_str());
  if (api_port_value == nullptr || api_port_value[0] == '\0')
    return;

  if (api::global_api_server == nullptr) {
    ESP_LOGW(TAG, "API server unavailable while applying %s", this->api_port_env_.c_str());
    return;
  }

  errno = 0;
  char *end = nullptr;
  long parsed = std::strtol(api_port_value, &end, 10);
  if (errno != 0 || end == api_port_value || *end != '\0' || parsed < 0 || parsed > 65535) {
    ESP_LOGE(TAG, "Invalid API port in %s: %s", this->api_port_env_.c_str(), api_port_value);
    return;
  }

  api::global_api_server->set_port(static_cast<uint16_t>(parsed));
  ESP_LOGI(TAG, "Overrode API port from %s", this->api_port_env_.c_str());

#ifdef USE_API_NOISE
  const char *noise_psk_value = std::getenv(this->noise_psk_env_.c_str());
  if (noise_psk_value != nullptr) {
    if (noise_psk_value[0] == '\0') {
      // Empty string: disable encryption
      api::global_api_server->set_noise_psk({});
      ESP_LOGI(TAG, "Disabled noise encryption from %s", this->noise_psk_env_.c_str());
    } else {
      // Base64-encoded PSK
      auto decoded = base64_decode(noise_psk_value);
#ifdef USE_NOISE
      // NoiseContext stores the pointer instead of copying, so the PSK must outlive setup()
      std::copy_n(decoded.begin(), std::min(decoded.size(), this->noise_psk_.size()), this->noise_psk_.begin());
      api::global_api_server->set_noise_psk(this->noise_psk_.data());
#else
      api::psk_t psk{};
      std::copy_n(decoded.begin(), std::min(decoded.size(), psk.size()), psk.begin());
      api::global_api_server->set_noise_psk(psk);
#endif
      ESP_LOGI(TAG, "Overrode noise PSK from %s", this->noise_psk_env_.c_str());
    }
  }
#endif  // USE_API_NOISE
}

void SerialxHostOverridesComponent::loop() {
  if (!this->ready_printed_) {
    fprintf(stderr, "Ready\n");
    fflush(stderr);
    this->ready_printed_ = true;
  }
}

}  // namespace esphome::serialx_host_overrides

#endif  // USE_HOST
