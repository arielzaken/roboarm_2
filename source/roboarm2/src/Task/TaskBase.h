#pragma once
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <stdint.h>

class TaskBase {
protected:
	TaskHandle_t _taskHandle = nullptr;
	const char* _taskName;
	UBaseType_t _priority;

public:
	TaskBase(const char* name, UBaseType_t priority = tskIDLE_PRIORITY + 1)
		: _taskName(name), _priority(priority) {}

	virtual ~TaskBase() {
		if (_taskHandle != nullptr) {
			vTaskDelete(_taskHandle);
			_taskHandle = nullptr;
		}
	}

	/** Launch dynamically allocated FreeRTOS task */
	bool startAsync(uint16_t stackSize) {
		BaseType_t res = xTaskCreate(
			&TaskBase::_taskTrampoline,
			_taskName,
			stackSize,
			this,
			_priority,
			&_taskHandle
		);
		return (res == pdPASS);
	}

	/** Launch statically allocated FreeRTOS task */
	TaskHandle_t startAsync(
		uint16_t stackSize,
		StackType_t* puxStackBuffer,
		StaticTask_t* pxTaskBuffer
	) {
		_taskHandle = xTaskCreateStatic(
			&TaskBase::_taskTrampoline,
			_taskName,
			stackSize,
			this,
			_priority,
			puxStackBuffer,
			pxTaskBuffer
		);
		return _taskHandle;
	}

	/** Check if task is running */
	bool isRunning() const { return _taskHandle != nullptr; }

	/** Getters */
	const char* getName() const { return _taskName; }
	UBaseType_t getPriority() const { return _priority; }
	TaskHandle_t getTaskHandle() const { return _taskHandle; }

protected:
	/** Derived class must implement the actual task entry */
	virtual void _taskEntry() = 0;

	inline void _kill(){
		// Clear handle under critical section
		_taskHandle = nullptr;

		vTaskDelete(nullptr);
	}
private:
	static void _taskTrampoline(void* param) {
		TaskBase* self = static_cast<TaskBase*>(param);

		// Pass the stored _param to the derived task entry
		self->_taskEntry();

		self->_kill();
	}
};
