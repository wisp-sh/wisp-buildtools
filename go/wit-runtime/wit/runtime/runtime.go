package runtime

import (
	"fmt"
	"runtime"
	"unsafe"
)

type Handle struct {
	value int32
}

func (h *Handle) Use() int32 {
	if h.value == 0 {
		panic("nil handle")
	}
	return h.value
}

func (h *Handle) Take() int32 {
	if h.value == 0 {
		panic("nil handle")
	}
	value := h.value
	h.value = 0
	return value
}

func (h *Handle) Set(value int32) {
	if value == 0 {
		panic("nil handle")
	}
	if h.value != 0 {
		panic("handle already set")
	}
	h.value = value
}

func (h *Handle) TakeOrNil() int32 {
	value := h.value
	h.value = 0
	return value
}

func MakeHandle(value int32) *Handle {
	if value == 0 {
		panic("nil handle")
	}
	return &Handle{value}
}

func Allocate(pinner *runtime.Pinner, size, align uintptr) unsafe.Pointer {
	pointer := allocateRaw(size, align)
	pinner.Pin(pointer)
	return pointer
}

func allocateRaw(size, align uintptr) unsafe.Pointer {
	if size == 0 {
		return nil
	}

	if size%align != 0 {
		panic(fmt.Sprintf("size %v is not compatible with alignment %v", size, align))
	}

	switch align {
	case 1:
		return unsafe.Pointer(unsafe.SliceData(make([]uint8, size)))
	case 2:
		return unsafe.Pointer(unsafe.SliceData(make([]uint16, size/align)))
	case 4:
		return unsafe.Pointer(unsafe.SliceData(make([]uint32, size/align)))
	case 8:
		return unsafe.Pointer(unsafe.SliceData(make([]uint64, size/align)))
	default:
		panic(fmt.Sprintf("unsupported alignment: %v", align))
	}
}

// NB: `cabi_realloc` may be called before the Go runtime has been initialized,
// in which case we need to use `runtime.sbrk` to do allocations.  The following
// is an abbreviation of [Till's
// efforts](https://github.com/bytecodealliance/go-modules/pull/367).

//go:linkname sbrk runtime.sbrk
func sbrk(n uintptr) unsafe.Pointer

//nolint:unused
var useGCAllocations = false

func init() {
	useGCAllocations = true
}

//nolint:unused
func offset(ptr, align uintptr) uintptr {
	newptr := (ptr + align - 1) &^ (align - 1)
	return newptr - ptr
}

var pinner = runtime.Pinner{}

func Unpin() {
	pinner.Unpin()
}

//nolint:unused
//go:wasmimport wasi_snapshot_preview1 adapter_monotonic_clock_set_paused
func adapterMonotonicClockSetPaused(paused bool)

//nolint:unused
//go:wasmexport cabi_realloc
func cabiRealloc(oldPointer unsafe.Pointer, oldSize, align, newSize uintptr) unsafe.Pointer {
	if oldPointer != nil || oldSize != 0 {
		panic("todo")
	}

	if useGCAllocations {
		// Here we call `adapter_monotonic_clock_set_paused` before and
		// after allocating since the Go garbage collector calls
		// `clock_time_get` to measure time spent in various stages of
		// GC, but calls to imports from `cabi_realloc` are forbidden by
		// the component model, so we must tell the
		// `wasi_snapshot_preview1` adapter to use a cached value
		// instead of calling `monotonic_clock::now`.
		//
		// See https://github.com/bytecodealliance/wasmtime/pull/13563
		// for more details.
		adapterMonotonicClockSetPaused(true)
		pointer := Allocate(&pinner, newSize, align)
		adapterMonotonicClockSetPaused(false)
		return pointer
	} else {
		alignedSize := newSize + offset(newSize, align)
		unaligned := sbrk(alignedSize)
		off := offset(uintptr(unaligned), align)
		return unsafe.Add(unaligned, off)
	}
}
