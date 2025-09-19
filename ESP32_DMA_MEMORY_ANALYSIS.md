# ESP32 DMA Memory Analysis

## The Problem
- **Available internal RAM**: 28,871 bytes total, 18,432 bytes largest block
- **DMA allocation failure**: Cannot allocate just 58 bytes for DMA
- **Timing**: Failure occurs ~84ms after memory check

## ESP32 Memory Architecture

### DMA-Capable Memory Constraints
On ESP32, not all internal RAM is DMA-capable:

1. **DRAM0**: 0x3FFB0000 - 0x3FFB7FFF (32KB) - DMA capable
2. **DRAM1**: 0x3FFB8000 - 0x3FFFFFFF (160KB) - DMA capable  
3. **IRAM**: 0x40080000 - 0x400A0000 (128KB) - NOT DMA capable
4. **RTC Memory**: NOT DMA capable

The key issue: **DMA allocations require memory from DRAM regions with specific alignment**

### Why 28KB Free Doesn't Help

The `heap_caps_get_free_size(MALLOC_CAP_INTERNAL)` includes:
- DRAM (DMA-capable)
- IRAM (NOT DMA-capable)
- Fragmented small blocks

But `esp_dma_capable_malloc()` can ONLY use:
- Contiguous DRAM blocks
- With proper alignment (typically 4-byte or cache-line)
- Not allocated for critical system functions

## Hidden Memory Consumers During Enumeration

### 1. FatFS Directory Operations
```c
DIR *dir = opendir(PATH_PREFIX);
```
Internally allocates:
- **DIR structure**: ~560 bytes
- **Sector buffer**: 512 bytes (or more for caching)
- **Long filename buffer**: Up to 255 * 2 bytes for Unicode
- **Cluster chain cache**: Variable, can be several KB

### 2. SD Card Driver Buffers
During `readdir()` calls:
- **DMA descriptors**: Multiple 12-byte descriptors
- **Transfer buffers**: Typically 4KB for optimal performance
- **Command buffers**: ~64 bytes per transaction

### 3. File System Cache
FatFS may allocate:
- **FAT cache**: 512 bytes to several KB
- **Directory entry cache**: 512 bytes minimum
- **Read-ahead buffers**: Configurable, often 2-4KB

## Memory Fragmentation Issue

The 84ms delay between log and failure suggests:
1. Multiple small allocations fragmenting DRAM
2. The 18KB "largest block" might be in IRAM (not DMA-capable)
3. DRAM is fragmented into blocks smaller than reported

## Diagnostic Approach

### 1. Check DMA-Specific Memory
```c
// More specific memory check
ESP_LOGI(TAG, "DMA-capable free: %d bytes, largest: %d bytes",
    heap_caps_get_free_size(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL),
    heap_caps_get_largest_free_block(MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL));
```

### 2. Track Memory During Enumeration
```c
// Before opendir
log_dma_memory("Before opendir");

DIR *dir = opendir(PATH_PREFIX);
log_dma_memory("After opendir");

// After first readdir
struct dirent *ent = readdir(dir);
log_dma_memory("After first readdir");
```

### 3. Monitor FatFS Configuration
Check sdkconfig for:
- `CONFIG_FATFS_LFN_HEAP` - Long filename allocation
- `CONFIG_FATFS_MAX_LFN` - Maximum filename length
- `CONFIG_WL_SECTOR_SIZE` - Sector size (affects buffers)

## Potential Solutions

### 1. Reduce FatFS Buffer Size
In sdkconfig:
```
CONFIG_FATFS_MAX_LFN=64  # Reduce from 255
CONFIG_FATFS_LFN_STACK=1  # Use stack instead of heap
```

### 2. Pre-allocate DMA Buffers
```c
// Reserve DMA memory before enumeration
void *dma_reserve = heap_caps_malloc(4096, MALLOC_CAP_DMA);
// Do enumeration
music_filenames_get(&files);
// Free reserve
heap_caps_free(dma_reserve);
```

### 3. Use Lower-Level APIs
Bypass FatFS directory functions:
```c
// Use raw sector reads with minimal buffers
esp_err_t read_directory_raw() {
    // Direct SD card sector reading
    // Manual FAT/directory parsing
}
```

### 4. Streaming Enumeration
Don't hold directory open:
```c
// Open, read one entry, close, repeat
for (int i = 0; i < max_entries; i++) {
    DIR *dir = opendir(PATH_PREFIX);
    seekdir(dir, i);  // Seek to position
    struct dirent *ent = readdir(dir);
    // Process entry
    closedir(dir);
}
```

## The Real Memory Usage

Based on the failure pattern, the actual memory consumption during enumeration is likely:
- **opendir()**: 2-3KB DMA memory
- **Each readdir()**: 1-2KB temporary DMA buffers
- **Peak usage**: 15-20KB of DMA-capable memory
- **Fragmentation overhead**: 5-10KB lost to small gaps

Total: **20-30KB of DMA-capable DRAM needed**, which matches the observed failure with 28KB total free.
