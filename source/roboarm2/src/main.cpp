#include <iostream>
#include "Sensors/PresenceSensor/PresenceSensor.h"
#include <esp_log.h>
#include "Task/TaskBase.h"

PresenceSensor ps0;
PresenceSensor ps1;
PresenceSensor ps2;
PresenceSensor ps3;

class Lisener : public PresenceSensor::observerType{
    void notification(bool val){
        ESP_LOGI("Lisener", "read %d", val);
    }
public:
};

Lisener lis;

extern "C" void app_main() {
    ps0.begin(GPIO_NUM_4);
    ps1.begin(GPIO_NUM_2);
    ps2.begin(GPIO_NUM_5);
    ps3.begin(GPIO_NUM_18);
    ps0.add_observer(lis);
    ps1.add_observer(lis);
    ps2.add_observer(lis);
    ps3.add_observer(lis);
}