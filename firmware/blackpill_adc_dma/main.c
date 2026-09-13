/**
 * @file main.c
 * @brief Blackpill (STM32F411CE) test firmware for PathScope.
 * @details ADC1 ch1 (PA1) continuous conversion -> DMA2 stream 0
 *          (channel 0) -> circular buffer in SRAM. KEY button (PA0,
 *          active low) toggles the DMA stream to inject the "stalled"
 *          and "overrun" anomalies the tool detects. PC13 LED blinks
 *          as a heartbeat. Bare metal, HSI 16 MHz, no HAL.
 */
#include <stdint.h>

#define REG(a)          (*(volatile uint32_t *)(a))

#define RCC_AHB1ENR     REG(0x40023830u)
#define RCC_APB2ENR     REG(0x40023844u)
#define GPIOA_MODER     REG(0x40020000u)
#define GPIOA_PUPDR     REG(0x4002000Cu)
#define GPIOA_IDR       REG(0x40020010u)
#define GPIOC_MODER     REG(0x40020800u)
#define GPIOC_ODR       REG(0x40020814u)
#define ADC1_SR         REG(0x40012000u)
#define ADC1_CR2        REG(0x40012008u)
#define ADC1_SMPR2      REG(0x40012010u)
#define ADC1_SQR3       REG(0x40012034u)
#define ADC1_DR_ADDR    (0x4001204Cu)
#define DMA2_S0CR       REG(0x40026410u)
#define DMA2_S0NDTR     REG(0x40026414u)
#define DMA2_S0PAR      REG(0x40026418u)
#define DMA2_S0M0AR     REG(0x4002641Cu)
#define DMA2_LIFCR      REG(0x40026408u)

#define BUF_LEN 1000u

static volatile uint16_t adc_buf[BUF_LEN];

/**
 * @brief Crude busy-wait delay.
 * @param n Loop iterations (about n/16M seconds at HSI).
 * @return None.
 */
static void delay(volatile uint32_t n) { while (n--) { } }

/**
 * @brief Start (or restart) DMA2 stream 0 for the ADC circular buffer.
 * @return None.
 */
static void dma_start(void)
{
    DMA2_S0CR &= ~1u;                       /* EN = 0 */
    while (DMA2_S0CR & 1u) { }
    DMA2_LIFCR = 0x3Du;                     /* clear stream 0 flags */
    DMA2_S0PAR = ADC1_DR_ADDR;
    DMA2_S0M0AR = (uint32_t)adc_buf;
    DMA2_S0NDTR = BUF_LEN;
    /* CHSEL=0, MSIZE=16, PSIZE=16, MINC, CIRC */
    DMA2_S0CR = (1u << 13) | (1u << 11) | (1u << 10) | (1u << 8);
    DMA2_S0CR |= 1u;                        /* EN */
    ADC1_SR &= ~(1u << 5);                  /* clear OVR */
    ADC1_CR2 |= (1u << 30);                 /* SWSTART again */
}

/**
 * @brief Firmware entry point: clocks, GPIO, ADC, DMA, button loop.
 * @return Never returns.
 */
int main(void)
{
    RCC_AHB1ENR |= (1u << 0) | (1u << 2) | (1u << 22); /* A, C, DMA2 */
    RCC_APB2ENR |= (1u << 8);                          /* ADC1 */

    GPIOA_MODER |= (3u << 2);               /* PA1 analog */
    GPIOA_PUPDR |= (1u << 0);               /* PA0 pull-up (KEY) */
    GPIOC_MODER |= (1u << 26);              /* PC13 output */

    ADC1_SMPR2 = (7u << 3);                 /* ch1: 480 cycles */
    ADC1_SQR3 = 1u;                         /* SQ1 = channel 1 */
    /* DDS + DMA + CONT + ADON */
    ADC1_CR2 = (1u << 9) | (1u << 8) | (1u << 1) | 1u;
    delay(1000);
    dma_start();

    uint32_t dma_on = 1u;
    for (;;) {
        GPIOC_ODR ^= (1u << 13);            /* heartbeat */
        if ((GPIOA_IDR & 1u) == 0u) {       /* KEY pressed */
            if (dma_on) {
                DMA2_S0CR &= ~1u;           /* stall: EN=0, OVR follows */
            } else {
                dma_start();
            }
            dma_on ^= 1u;
            delay(2000000);                 /* debounce + release wait */
        }
        delay(400000);
    }
}

/** @brief Initial stack pointer + reset vector table. */
__attribute__((section(".vectors")))
const uint32_t vectors[] = {
    0x20020000u,                            /* MSP top of 128K SRAM */
    (uint32_t)main,
};
