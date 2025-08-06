#pragma once
#include <etl/observer.h>
#include <config.h>

template <typename T>
class ISensor : public etl::observable<etl::observer<T>, MAX_OBSERVERS_PER_SENSOR>{
public:
    using observerType = etl::observer<T>;
    virtual T read() = 0;
};