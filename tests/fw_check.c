/* Runs the radio firmware's own parser (p4-radio main/station_parse.c) over
 * the files in a stick's radio/ folder and prints what the radio would store.
 * Built and run by tests/test_stick.py:
 *   gcc -include fw_shim.h -Dstrlcpy=test_strlcpy -I<fw>/main fw_check.c <fw>/main/station_parse.c
 * Output, one item per line:
 *   S <name>\t<url>        a station           R <line>\t<reason>   a rejected line
 *   W <ssid>\t<password>   wifi.txt accepted   W! wifi.txt refused
 *   C <key>\t<value>       a config item       CR <line>\t<reason>  a rejected config line
 *   TZ ok|bad  B <n>       timezone check, brightness (-1 = bad)
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "station_parse.h"

size_t test_strlcpy(char *dst, const char *src, size_t size)
{
    size_t n = strlen(src);
    if (size > 0) {
        size_t c = n >= size ? size - 1 : n;
        memcpy(dst, src, c);
        dst[c] = '\0';
    }
    return n;
}

static char *slurp(const char *dir, const char *name)
{
    char path[1024];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "rb");
    if (f == NULL) {
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *b = malloc((size_t)n + 1);
    if (fread(b, 1, (size_t)n, f) != (size_t)n) {
        n = 0;
    }
    b[n] = '\0';
    fclose(f);
    return b;
}

static void on_reject(int line_no, station_reject_t why, const char *text, void *ctx)
{
    (void)text; (void)ctx;
    printf("R %d\t%s\n", line_no, station_reject_reason(why));
}

static void on_item(int line_no, const char *key, const char *value, void *ctx)
{
    (void)line_no; (void)ctx;
    printf("C %s\t%s\n", key, value);
    if (strcmp(key, "timezone") == 0) {
        printf("TZ %s\n", config_timezone_valid(value) ? "ok" : "bad");
    } else if (strcmp(key, "brightness") == 0) {
        printf("B %d\n", config_parse_brightness(value));
    }
}

static void on_config_reject(int line_no, const char *reason, const char *text, void *ctx)
{
    (void)text; (void)ctx;
    printf("CR %d\t%s\n", line_no, reason);
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: fw_check STICK/radio\n");
        return 2;
    }
    static station_t list[STATION_MAX_COUNT];
    char *t = slurp(argv[1], "stations.txt");
    if (t != NULL) {
        int n = parse_stations_text_ex(t, list, STATION_MAX_COUNT, on_reject, NULL);
        for (int i = 0; i < n; i++) {
            printf("S %s\t%s\n", list[i].name, list[i].url);
        }
    }
    t = slurp(argv[1], "wifi.txt");
    if (t != NULL) {
        char ssid[WIFI_SSID_MAX + 1], pass[WIFI_PASS_MAX + 1];
        if (parse_wifi_text(t, ssid, pass)) {
            printf("W %s\t%s\n", ssid, pass);
        } else {
            printf("W!\n");
        }
    }
    t = slurp(argv[1], "config.txt");
    if (t != NULL) {
        parse_config_text(t, on_item, on_config_reject, NULL);
    }
    return 0;
}
