#pragma once
#include "../ISensor.h"
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

class PresenceSensor : public ISensor<bool>{
    QueueHandle_t _queue = nullptr;
    gpio_num_t _pin;
public:
    PresenceSensor(gpio_num_t pin);
    bool read() override; 
    static void _ISRservice(void* param);
};