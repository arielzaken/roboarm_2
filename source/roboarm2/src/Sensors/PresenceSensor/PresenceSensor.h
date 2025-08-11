// PresenceSensor.h
#pragma once
#include "../ISensor.h"
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <soc/gpio_num.h>
#include "Task/TaskBase.h"
#include "esp_cpu.h"
#include "esp_rom_sys.h"

/**
 *	@brief Presence sensor with ISR-only debounce (1ms)
 */
class PresenceSensor :
	public ISensor<bool>,
	public TaskBase
{
	gpio_num_t _pin = GPIO_NUM_NC;

    bool _last;

	void _taskEntry() override;

public:
	PresenceSensor() : TaskBase("PresenceSensor") {}
	void begin(gpio_num_t pin);
	bool read() override;
	static void IRAM_ATTR _ISRservice(void* arg);
};
