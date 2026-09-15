# f411 target files

The reference target: STM32F411 (Blackpill board), used by the demo
and the hardware test suite.

## File provenance and licensing

- `f411.topology.yaml`, `f411.flows.yaml` - written for this project,
  MIT like the rest of the repo.
- `STM32F411.svd` - STMicroelectronics' CMSIS-SVD description of the
  STM32F411 device, copyright STMicroelectronics. It is redistributed
  here unmodified, for out-of-the-box convenience only, and is NOT
  covered by this repository's MIT license - ST's own terms apply.
  The authoritative copy comes from ST (st.com) or from the Keil
  device family pack that `pyocd pack install stm32f411ce` downloads;
  if redistribution here is ever a concern, delete the file and point
  the target at a locally fetched copy instead.
