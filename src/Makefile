obj-m += nova_kmod.o

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD := $(shell pwd)

.PHONY: all module sender clean
all: module sender

module:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

sender: nova_desktop.c nova_kmod_uapi.h
	$(CC) -O3 -Wall -Wextra -o nova_desktop nova_desktop.c

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
	$(RM) nova_desktop
