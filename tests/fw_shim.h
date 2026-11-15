/* Forced into station_parse.c when it is built on a PC by tests/test_stick.py:
 * older glibc has no strlcpy, so the firmware's calls are renamed to this one. */
#include <stddef.h>
size_t test_strlcpy(char *dst, const char *src, size_t size);
