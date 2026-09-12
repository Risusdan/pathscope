CC = arm-none-eabi-gcc
CFLAGS = -mcpu=cortex-m4 -mthumb -O2 -Wall -Wextra -ffreestanding \
         -nostdlib -T stm32f411.ld

all: fw.elf

fw.elf: main.c stm32f411.ld
	$(CC) $(CFLAGS) main.c -o $@

flash: fw.elf
	../../.venv/bin/pyocd flash --target stm32f411ce fw.elf

clean:
	rm -f fw.elf
