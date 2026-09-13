CC = arm-none-eabi-gcc
CFLAGS = -mcpu=cortex-m4 -mthumb -O2 -Wall -Wextra -ffreestanding \
         -nostdlib -I../ps_trace -T stm32f411.ld

PS_TRACE_DIR = ../ps_trace

all: fw.elf

fw.elf: main.c $(PS_TRACE_DIR)/ps_trace.c $(PS_TRACE_DIR)/ps_trace.h stm32f411.ld
	$(CC) $(CFLAGS) main.c $(PS_TRACE_DIR)/ps_trace.c -o $@

flash: fw.elf
	../../.venv/bin/pyocd flash --target stm32f411ce fw.elf

clean:
	rm -f fw.elf
