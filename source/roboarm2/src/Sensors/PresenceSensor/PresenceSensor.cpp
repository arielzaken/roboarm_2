#include "PresenceSensor.h"
#include <esp_log.h>
#include <config.h>
#include <driver/gpio.h>
#include <esp_intr_alloc.h>
#include <utill.h>

static const char* TAG = "PresenceSensor";

PresenceSensor::PresenceSensor(gpio_num_t pin) : _pin(pin)
{
    _queue = xQueueCreate(
        PRESENCE_SENSOR_QUEUE_SIZE,
        sizeof(bool)
    );

    if(!_queue){
        ESP_LOGE(TAG, "failed to create a queue.");
        _pin = GPIO_NUM_NC;
        return;
    }

    gpio_config_t io_config = {
        .pin_bit_mask = BIT(pin),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_ANYEDGE
    };
    gpio_config(&io_config);

    gpio_isr_register(
        _ISRservice,
        &(this->_queue),
        ESP_INTR_FLAG_EDGE | ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LOWMED,
        nullptr
    );

    gpio_intr_enable(pin);
    ESP_LOGD(TAG, "created at %p with gpio %d", this, pin);
}

bool PresenceSensor::read()
{
    return false;
}


void IRAM_ATTR PresenceSensor::_ISRservice(void* param)
{
    
}
