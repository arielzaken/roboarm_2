#include <driver/rmt_tx.h>
#include <FreeRTOS/FreeRTOS.h>
// Demo
constexpr uint32_t TICKS_PER_S = 16'000'000;
rmt_channel_handle_t tx_chan = NULL;

extern "C" void app_main(void) {
    
    rmt_tx_channel_config_t tx_chan_config;
    tx_chan_config.gpio_num = GPIO_NUM_5;
    tx_chan_config.clk_src = rmt_clock_source_t::RMT_CLK_SRC_DEFAULT;
    tx_chan_config.resolution_hz = TICKS_PER_S;
    tx_chan_config.mem_block_symbols = 128;
    tx_chan_config.trans_queue_depth = 32;
    tx_chan_config.intr_priority = 0;
    tx_chan_config.flags.invert_out = false;
    ESP_ERROR_CHECK(rmt_new_tx_channel(&tx_chan_config, &tx_chan));

    rmt_encoder_handle_t encoder;
    rmt_copy_encoder_config_t copy_encoder_config;

    ESP_ERROR_CHECK(rmt_new_copy_encoder(&copy_encoder_config, &encoder));
    rmt_symbol_word_t symbol;
    rmt_transmit_config_t rmt_transmit_conf;
    rmt_transmit_conf.loop_count = -1;
    rmt_transmit_conf.flags.eot_level = 0;
    rmt_transmit_conf.flags.queue_nonblocking = true;
    symbol.level0 = 0;
    symbol.level1 = 1;
    
    for(int32_t duration = 0x3ff; duration >= 0xff ; duration -= 0x18){
        symbol.duration0 = duration;
        symbol.duration1 = duration;
        ESP_ERROR_CHECK(rmt_transmit(tx_chan, encoder, &symbol, sizeof(symbol), &rmt_transmit_conf));
        ESP_ERROR_CHECK(rmt_enable(tx_chan));
        vTaskDelay(pdMS_TO_TICKS(500));
        ESP_ERROR_CHECK(rmt_disable(tx_chan));
    }
    for(int32_t duration = 0xff; duration >= 1 ; duration -= 1){
        symbol.duration0 = duration;
        symbol.duration1 = duration;
        ESP_ERROR_CHECK(rmt_transmit(tx_chan, encoder, &symbol, sizeof(symbol), &rmt_transmit_conf));
        ESP_ERROR_CHECK(rmt_enable(tx_chan));
        vTaskDelay(pdMS_TO_TICKS(1000));
        ESP_ERROR_CHECK(rmt_disable(tx_chan));
    }
    symbol.duration0 = 1;
    symbol.duration1 = 1;
    ESP_ERROR_CHECK(rmt_enable(tx_chan));
    ESP_ERROR_CHECK(rmt_transmit(tx_chan, encoder, &symbol, sizeof(symbol), &rmt_transmit_conf));
}
