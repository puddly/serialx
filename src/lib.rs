use pyo3::prelude::*;

#[cfg(target_os = "macos")]
mod darwin;

#[cfg(target_os = "windows")]
mod windows;

#[pyclass(get_all)]
pub struct RustSerialPortInfo {
    pub device: String,
    pub vid: Option<u16>,
    pub pid: Option<u16>,
    pub serial_number: Option<String>,
    pub manufacturer: Option<String>,
    pub product: Option<String>,
    pub bcd_device: Option<u16>,
    pub interface_description: Option<String>,
    pub interface_num: Option<u8>,
}

#[cfg(target_os = "macos")]
#[pyfunction]
fn list_serial_ports_impl() -> PyResult<Vec<RustSerialPortInfo>> {
    darwin::list_serial_ports().map_err(PyErr::new::<pyo3::exceptions::PyOSError, _>)
}

#[cfg(target_os = "windows")]
#[pyfunction]
fn list_serial_ports_impl() -> PyResult<Vec<RustSerialPortInfo>> {
    windows::list_serial_ports().map_err(PyErr::new::<pyo3::exceptions::PyOSError, _>)
}

#[pymodule]
#[allow(unused_variables, clippy::missing_const_for_fn)]
fn _serialx_rust(m: &Bound<'_, PyModule>) -> PyResult<()> {
    #[cfg(any(target_os = "macos", target_os = "windows"))]
    {
        m.add_class::<RustSerialPortInfo>()?;
        m.add_function(wrap_pyfunction!(list_serial_ports_impl, m)?)?;
    }
    Ok(())
}
