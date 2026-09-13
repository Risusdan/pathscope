/**
 * @file main.c
 * @brief Blackpill (STM32F411CE) test firmware for PathScope.
 * @details ADC1 ch1 (PA1) continuous conversion -> DMA2 stream 0
 *          (channel 0) -> circular buffer in SRAM. KEY button (PA0,
 *          active low) toggles the DMA stream to inject the "stalled"
 *          and "overrun" anomalies the tool detects. PC13 LED blinks
 *          as a heartbeat. TIM2 update interrupt at 1 kHz drives the
 *          ps_trace sampler (see ../ps_trace/ps_trace.h) so the
 *          PathScope host can read a trace-buffer scope over the
 *          debug probe. Bare metal, HSI 16 MHz, no HAL.
 */
#include <stdint.h>

#include "ps_trace.h"

#define REG(a)          (*(volatile uint32_t *)(a))

#define RCC_AHB1ENR     REG(0x40023830u)
#define RCC_APB1ENR     REG(0x40023840u)
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
#define TIM2_CR1        REG(0x40000000u)
#define TIM2_DIER       REG(0x4000000Cu)
#define TIM2_SR         REG(0x40000010u)
#define TIM2_PSC        REG(0x40000028u)
#define TIM2_ARR        REG(0x4000002Cu)
#define NVIC_ISER0      REG(0xE000E100u)

#define BUF_LEN 1000u

static volatile uint16_t adc_buf[BUF_LEN];

/** @brief Ranges ps_trace_sample() may read: SRAM, DMA2, ADC1
 *         registers - covers adc_buf and the peripherals it drives. */
const ps_trace_range_t ps_trace_whitelist[] = {
    { 0x20000000u, 0x20020000u },  /* SRAM */
    { 0x40026400u, 0x40026500u },  /* DMA2 */
    { 0x40012000u, 0x40012100u },  /* ADC1 */
};
const uint32_t ps_trace_whitelist_len =
    sizeof(ps_trace_whitelist) / sizeof(ps_trace_whitelist[0]);

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

    ps_trace_init();                        /* publish descriptor before
                                                the sample tick starts */

    RCC_APB1ENR |= (1u << 0);               /* TIM2EN */
    TIM2_PSC = 15u;                         /* HSI 16 MHz / 16 = 1 MHz */
    TIM2_ARR = 999u;                        /* 1 MHz / 1000 = 1 kHz update */
    TIM2_SR = 0u;                           /* clear any pending UIF */
    TIM2_DIER |= (1u << 0);                 /* UIE */
    NVIC_ISER0 = (1u << 28);                /* enable TIM2 IRQ (IRQn 28) */
    TIM2_CR1 |= (1u << 0);                  /* CEN: start counting */

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

/**
 * @brief TIM2 update ISR: the ps_trace 1 kHz sample tick.
 * @return None.
 */
void TIM2_IRQHandler(void)
{
    TIM2_SR &= ~(1u << 0);                  /* clear UIF */
    ps_trace_sample();
}

/** @brief Catch-all for exceptions/interrupts this firmware does not use. */
static void Default_Handler(void) { for (;;) { } }

/* Shorthand so the vector table below stays one column wide. */
#define DH (uint32_t)Default_Handler

/**
 * @brief Initial stack pointer + full vector table through TIM2
 *        (IRQn 28), Cortex-M4 standard layout. Reserved exception
 *        slots are 0 per the architecture; unused faults/IRQs point
 *        at Default_Handler.
 */
__attribute__((section(".vectors")))
const uint32_t vectors[] = {
    0x20020000u,                            /* MSP: top of 128K SRAM */
    (uint32_t)main,                         /* Reset */
    DH,                                     /* NMI */
    DH,                                     /* HardFault */
    DH,                                     /* MemManage */
    DH,                                     /* BusFault */
    DH,                                     /* UsageFault */
    0u, 0u, 0u, 0u,                         /* Reserved x4 */
    DH,                                     /* SVCall */
    DH,                                     /* Debug Monitor */
    0u,                                     /* Reserved */
    DH,                                     /* PendSV */
    DH,                                     /* SysTick */
    DH,                                     /* IRQ0  WWDG */
    DH,                                     /* IRQ1  PVD */
    DH,                                     /* IRQ2  TAMP_STAMP */
    DH,                                     /* IRQ3  RTC_WKUP */
    DH,                                     /* IRQ4  FLASH */
    DH,                                     /* IRQ5  RCC */
    DH,                                     /* IRQ6  EXTI0 */
    DH,                                     /* IRQ7  EXTI1 */
    DH,                                     /* IRQ8  EXTI2 */
    DH,                                     /* IRQ9  EXTI3 */
    DH,                                     /* IRQ10 EXTI4 */
    DH,                                     /* IRQ11 DMA1_Stream0 */
    DH,                                     /* IRQ12 DMA1_Stream1 */
    DH,                                     /* IRQ13 DMA1_Stream2 */
    DH,                                     /* IRQ14 DMA1_Stream3 */
    DH,                                     /* IRQ15 DMA1_Stream4 */
    DH,                                     /* IRQ16 DMA1_Stream5 */
    DH,                                     /* IRQ17 DMA1_Stream6 */
    DH,                                     /* IRQ18 ADC */
    DH,                                     /* IRQ19 CAN1_TX */
    DH,                                     /* IRQ20 CAN1_RX0 */
    DH,                                     /* IRQ21 CAN1_RX1 */
    DH,                                     /* IRQ22 CAN1_SCE */
    DH,                                     /* IRQ23 EXTI9_5 */
    DH,                                     /* IRQ24 TIM1_BRK_TIM9 */
    DH,                                     /* IRQ25 TIM1_UP_TIM10 */
    DH,                                     /* IRQ26 TIM1_TRG_COM_TIM11 */
    DH,                                     /* IRQ27 TIM1_CC */
    (uint32_t)TIM2_IRQHandler,              /* IRQ28 TIM2 */
};
