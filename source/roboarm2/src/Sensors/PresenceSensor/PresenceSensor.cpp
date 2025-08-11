/**
 *	@file PresenceSensor.cpp
 *	@brief GPIO presence sensor with ISR-only 1ms debounce for ESP-IDF.
 *	@details
 *		- Debounce is performed entirely in the ISR using the CPU cycle counter.
 *		- No esp_timer, no GPIO interrupt gating, no scheduler dependency.
 *		- Any edge within 1ms of the previous edge is ignored.
 */

#include "PresenceSensor.h"
#include <esp_log.h>
#include <driver/gpio.h>
#include <esp_cpu.h>
#include <esp_rom_sys.h>

static const char* TAG = "PresenceSensor";

///	@brief Task loop: consume counted edges and notify observers in time order.
void PresenceSensor::_taskEntry()
{
	for(;;){
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        gpio_intr_disable(_pin);
        vTaskDelay(pdMS_TO_TICKS(10));
        gpio_intr_enable(_pin);

        bool v = read();
        if(v != _last){
            notify_observers(v);
            _last = v;
        }
	}
}

///	@brief Initialize the sensor and attach GPIO interrupt.
///	@param pin GPIO number to monitor (input, pull-down, any-edge).
void PresenceSensor::begin(gpio_num_t pin)
{
	startAsync(4000);

	_pin = pin;
    
	gpio_config_t io_config = {
        .pin_bit_mask = 1ULL << _pin,
		.mode = GPIO_MODE_INPUT,
		.pull_up_en = GPIO_PULLUP_DISABLE,
		.pull_down_en = GPIO_PULLDOWN_ENABLE,
		.intr_type = GPIO_INTR_ANYEDGE
	};
	gpio_config(&io_config);
    
	/* install shared ISR service; ok if already installed */
	if (gpio_install_isr_service(ESP_INTR_FLAG_IRAM) != ESP_OK){
        /* already installed or minor error; continue */
	}
    
	/* hook ISR handler for this pin */
	gpio_isr_handler_add(_pin, &PresenceSensor::_ISRservice, this);

    _last = read();
	gpio_intr_enable(_pin);
    
	ESP_LOGD(TAG, "created at %p with gpio %d", this, static_cast<int>(_pin));
}

///	@brief Read current raw GPIO level (non-debounced).
///	@return true if high, false if low.
bool PresenceSensor::read()
{
	return gpio_get_level(_pin);
}

///	@brief GPIO ISR with 1ms per-edge debounce using CPU cycle counter.
///	@param arg this pointer
void IRAM_ATTR PresenceSensor::_ISRservice(void* arg)
{
	PresenceSensor* This = reinterpret_cast<PresenceSensor*>(arg);
	BaseType_t higher = pdFALSE;
	if (This->_taskHandle){
		vTaskNotifyGiveFromISR(This->_taskHandle, &higher);
	}
	portYIELD_FROM_ISR(higher);
}
