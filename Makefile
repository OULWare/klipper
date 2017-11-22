# Klipper build system
#
# Copyright (C) 2016,2017  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

# Output directory
OUT=out/

# Kconfig includes
export HOSTCC             := $(CC)
export CONFIG_SHELL       := sh
export KCONFIG_AUTOHEADER := autoconf.h
export KCONFIG_CONFIG     := $(CURDIR)/.config
-include $(KCONFIG_CONFIG)

# Common command definitions
CC=$(CROSS_PREFIX)gcc
AS=$(CROSS_PREFIX)gcc
#AS=$(CROSS_PREFIX)as
LD=$(CROSS_PREFIX)ld
OBJCOPY=$(CROSS_PREFIX)objcopy
OBJDUMP=$(CROSS_PREFIX)objdump
STRIP=$(CROSS_PREFIX)strip
CPP=cpp
PYTHON=python2

# Source files
src-y =
asm-y =
dirs-y = src

# Default compiler flags
cc-option=$(shell if test -z "`$(1) $(2) -S -o /dev/null -xc /dev/null 2>&1`" \
    ; then echo "$(2)"; else echo "$(3)"; fi ;)

CFLAGS := -I$(OUT) -Isrc -I$(OUT)board-generic/ -O2 -MD -g \
    -Wall -Wold-style-definition $(call cc-option,$(CC),-Wtype-limits,) \
    -ffunction-sections -fdata-sections
CFLAGS += -flto -fwhole-program
ifneq ($(CONFIG_MACH_LPC176X),y)
  CFLAGS += -fno-use-linker-plugin
endif

CFLAGS_klipper.elf = $(CFLAGS) -Wl,--gc-sections

CPPFLAGS = -I$(OUT) -P -MD -MT $@

# Default targets
target-y := $(OUT)klipper.elf

all:

# Run with "make V=1" to see the actual compile commands
ifdef V
Q=
else
Q=@
MAKEFLAGS += --no-print-directory
endif

# Include board specific makefile
include src/Makefile
-include src/$(patsubst "%",%,$(CONFIG_BOARD_DIRECTORY))/Makefile

################ Common build rules

$(OUT)%.o: %.c $(OUT)autoconf.h $(OUT)board-link
	@echo "  Compiling C   $@"
	$(Q)$(CC) $(CFLAGS) -c $< -o $@

$(OUT)%.o: %.s $(OUT)autoconf.h $(OUT)board-link
	@echo "  Compiling ASM $@"
	$(Q)$(AS) $(ASFLAGS) -c $< -o $@

################ Main build rules

$(OUT)board-link: $(KCONFIG_CONFIG)
	@echo "  Creating symbolic link $(OUT)board"
	$(Q)mkdir -p $(addprefix $(OUT), $(dirs-y))
	$(Q)touch $@
	$(Q)ln -Tsf $(PWD)/src/$(CONFIG_BOARD_DIRECTORY) $(OUT)board
	$(Q)mkdir -p $(OUT)board-generic
	$(Q)ln -Tsf $(PWD)/src/generic $(OUT)board-generic/board

$(OUT)%.o.ctr: $(OUT)%.o
	$(Q)$(OBJCOPY) -j '.compile_time_request' -O binary $^ $@

$(OUT)compile_time_request.o: $(patsubst %.c, $(OUT)src/%.o.ctr,$(src-y)) ./scripts/buildcommands.py
	@echo "  Building $@"
	$(Q)cat $(patsubst %.c, $(OUT)src/%.o.ctr,$(src-y)) > $(OUT)klipper.compile_time_request
	$(Q)$(PYTHON) ./scripts/buildcommands.py -d $(OUT)klipper.dict $(OUT)klipper.compile_time_request $(OUT)compile_time_request.c
	$(Q)$(CC) $(CFLAGS) -c $(OUT)compile_time_request.c -o $@

$(OUT)klipper.elf: $(patsubst %.c, $(OUT)src/%.o,$(src-y)) $(OUT)compile_time_request.o  $(patsubst %.s, $(OUT)src/%.o,$(asm-y))
	@echo "  Linking $@"
	$(Q)$(CC) $(CFLAGS_klipper.elf) $^ -o $@

################ Kconfig rules

define do-kconfig
$(Q)rm -rf $(OUT)
$(Q)mkdir -p $(OUT)/scripts/kconfig/lxdialog
$(Q)mkdir -p $(OUT)/include/config
$(Q)$(MAKE) -C $(OUT) -f $(CURDIR)/scripts/kconfig/Makefile srctree=$(CURDIR) src=scripts/kconfig obj=scripts/kconfig Q=$(Q) Kconfig=$(CURDIR)/src/Kconfig $1
endef

$(OUT)autoconf.h : $(KCONFIG_CONFIG) ; $(call do-kconfig, silentoldconfig)
$(KCONFIG_CONFIG): src/Kconfig ; $(call do-kconfig, olddefconfig)
%onfig: ; $(call do-kconfig, $@)
help: ; $(call do-kconfig, $@)


################ Generic rules

# Make definitions
.PHONY : all clean distclean FORCE
.DELETE_ON_ERROR:

all: $(target-y)

clean:
	$(Q)rm -rf $(OUT)

distclean: clean
	$(Q)rm -f .config .config.old

-include $(OUT)*.d $(patsubst %,$(OUT)%/*.d,$(dirs-y))
