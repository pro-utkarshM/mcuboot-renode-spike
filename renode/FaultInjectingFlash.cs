// SPDX-License-Identifier: MIT
//
// A deliberately small nRF52840 internal-flash/NVMC model for Renode 1.16.1.
// It observes CPU writes at the emulator bus boundary. Firmware does not call
// into this model and cannot manufacture its operation records.

using System;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;

using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.Memory
{
    // Renode's regular memories deliberately retain their contents on a
    // machine reset. A power cut must not: the flash model explicitly calls
    // ClearForPowerLoss before requesting the reset.
    public sealed class PowerLossRam : ArrayMemory
    {
        public PowerLossRam(ulong size) : base(size)
        {
        }

        public void ClearForPowerLoss()
        {
            Array.Clear(array, 0, array.Length);
        }
    }

    // ArrayMemory is required here. MappedMemory accesses stay in Renode's C
    // translation layer and therefore cannot provide a trustworthy C# flash
    // operation hook.
    public sealed class FaultInjectingFlash : ArrayMemory
    {
        public FaultInjectingFlash(IMachine machine, PowerLossRam ram, ulong size,
            int pageSize) : base(size, ErasedValue)
        {
            if(pageSize <= 0 || size % (ulong)pageSize != 0)
            {
                throw new ArgumentException("Flash size must be a multiple of pageSize");
            }

            this.machine = machine;
            this.ram = ram;
            this.pageSize = pageSize;
        }

        public override void WriteDoubleWord(long offset, uint value)
        {
            Program(offset, BitConverter.GetBytes(value));
        }

        public override void WriteWord(long offset, ushort value)
        {
            Program(offset, BitConverter.GetBytes(value));
        }

        public override void WriteByte(long offset, byte value)
        {
            Program(offset, new[] { value });
        }

        public void SetWriteEnabled(bool enabled)
        {
            writeEnabled = enabled;
        }

        public void ErasePage(long address)
        {
            if(!eraseEnabled)
            {
                this.Log(LogLevel.Warning,
                    "Ignoring page erase at 0x{0:X8}: NVMC is not in erase mode", address);
                return;
            }
            if(address < 0 || address % pageSize != 0 || address + pageSize > Size)
            {
                this.Log(LogLevel.Error, "Ignoring invalid page erase at 0x{0:X8}", address);
                return;
            }

            lock(sync)
            {
                var captureEvidence = NextOperationTriggersFault();
                byte[] before = null;
                if(captureEvidence)
                {
                    before = new byte[pageSize];
                    Array.Copy(array, checked((int)address), before, 0, pageSize);
                }
                Array.Fill(array, ErasedValue, checked((int)address), pageSize);
                Persist(address, array, checked((int)address), pageSize);
                byte[] after = null;
                if(captureEvidence)
                {
                    after = new byte[pageSize];
                    Array.Copy(array, checked((int)address), after, 0, pageSize);
                }
                CompletedOperation("erase", address, pageSize, before, after);
            }
        }

        public void EraseAll()
        {
            if(!eraseEnabled)
            {
                this.Log(LogLevel.Warning, "Ignoring mass erase: NVMC is not in erase mode");
                return;
            }

            // A mass erase is represented as the physical page erases it
            // contains, preserving the graded cut boundary.
            for(var address = 0L; address < Size; address += pageSize)
            {
                ErasePage(address);
            }
        }

        public void SetEraseEnabled(bool enabled)
        {
            eraseEnabled = enabled;
        }

        // LoadFlash is a host provisioning operation and is intentionally not
        // routed through Program/ErasePage or included in the guest trace.
        public void LoadFlash(string path)
        {
            var bytes = File.ReadAllBytes(path);
            if(bytes.LongLength != Size)
            {
                throw new ArgumentException(string.Format(
                    CultureInfo.InvariantCulture,
                    "Flash image '{0}' has {1} bytes, expected {2}",
                    path, bytes.LongLength, Size));
            }

            lock(sync)
            {
                if(backingStream != null)
                {
                    backingStream.Dispose();
                }
                Array.Copy(bytes, array, bytes.Length);
                backingPath = Path.GetFullPath(path);
                // Disable FileStream's managed buffer. Every guest operation
                // still reaches the host kernel as an individual write, but
                // keeping the descriptor open avoids tens of thousands of
                // open/close cycles during an MCUboot swap.
                backingStream = new FileStream(backingPath, FileMode.Open,
                    FileAccess.Write, FileShare.Read, 1);
            }
        }

        public void SaveFlash(string path)
        {
            lock(sync)
            {
                File.WriteAllBytes(path, array);
            }
        }

        public void BeginTraceFromEnvironment(string path)
        {
            var value = Environment.GetEnvironmentVariable("FAULT_AFTER_OPERATION");
            long faultAfter = 0;
            if(!string.IsNullOrWhiteSpace(value)
                && !long.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out faultAfter))
            {
                throw new ArgumentException(
                    "FAULT_AFTER_OPERATION must be an unsigned decimal operation number");
            }
            BeginTrace(path, faultAfter);
        }

        public void BeginTrace(string path, long faultAfter)
        {
            if(faultAfter < 0)
            {
                throw new ArgumentOutOfRangeException(nameof(faultAfter));
            }

            lock(sync)
            {
                if(traceWriter != null)
                {
                    traceWriter.Dispose();
                }
                tracePath = Path.GetFullPath(path);
                var directory = Path.GetDirectoryName(tracePath);
                if(!string.IsNullOrEmpty(directory))
                {
                    Directory.CreateDirectory(directory);
                }
                traceWriter = new StreamWriter(new FileStream(tracePath,
                    FileMode.Create, FileAccess.Write, FileShare.Read));
                // The controller observes the trace while Renode is running
                // so it can detect the exact configured fault boundary.
                traceWriter.AutoFlush = true;
                operation = 0;
                powerLossCount = 0;
                faultAfterOperation = faultAfter;
                faultFired = false;
                traceEnabled = true;
            }
        }

        public void EndTrace()
        {
            lock(sync)
            {
                traceEnabled = false;
                if(traceWriter != null)
                {
                    traceWriter.Dispose();
                    traceWriter = null;
                }
            }
        }

        public long OperationCount => operation;

        public long PowerLossCount => powerLossCount;

        public long FaultAfterOperation => faultAfterOperation;

        public bool FaultFired => faultFired;

        private void Program(long offset, byte[] requested)
        {
            if(!writeEnabled)
            {
                this.Log(LogLevel.Warning,
                    "Ignoring program at 0x{0:X8}: NVMC is not in write mode", offset);
                return;
            }
            if(offset < 0 || offset + requested.Length > Size)
            {
                this.Log(LogLevel.Error, "Ignoring invalid program at 0x{0:X8}", offset);
                return;
            }

            lock(sync)
            {
                var captureEvidence = NextOperationTriggersFault();
                byte[] before = null;
                if(captureEvidence)
                {
                    before = new byte[requested.Length];
                    Array.Copy(array, checked((int)offset), before, 0, requested.Length);
                }
                for(var i = 0; i < requested.Length; ++i)
                {
                    // Nordic internal flash can only transition one bits to
                    // zero bits until the enclosing page is erased.
                    var index = checked((int)offset + i);
                    array[index] = (byte)(array[index] & requested[i]);
                }
                Persist(offset, array, checked((int)offset), requested.Length);
                byte[] after = null;
                if(captureEvidence)
                {
                    after = new byte[requested.Length];
                    Array.Copy(array, checked((int)offset), after, 0, requested.Length);
                }
                CompletedOperation("program", offset, requested.Length, before, after);
            }
        }

        private bool NextOperationTriggersFault()
        {
            return traceEnabled && !faultFired
                && faultAfterOperation == operation + 1;
        }

        private void Persist(long offset, byte[] source, int sourceOffset, int count)
        {
            if(backingStream == null)
            {
                throw new InvalidOperationException(
                    "LoadFlash must establish a nonvolatile backing file before guest execution");
            }

            backingStream.Position = offset;
            backingStream.Write(source, sourceOffset, count);
            // The stream has no managed buffer, so the completed write is
            // visible through the backing file before CompletedOperation can
            // inject a simulated-MCU reset. Host stable-media fsync is outside
            // the modeled fault domain.
        }

        private void CompletedOperation(string type, long address, int length,
            byte[] before, byte[] after)
        {
            if(!traceEnabled)
            {
                return;
            }

            operation++;
            var record = string.Format(CultureInfo.InvariantCulture,
                "op={0} type={1} address=0x{2:X8} length={3}",
                operation, type, address, length);
            traceWriter.WriteLine(record);

            if(faultAfterOperation == operation && !faultFired)
            {
                if(before == null || after == null)
                {
                    throw new InvalidOperationException(
                        "selected fault operation lacks before/after evidence");
                }
                // The flash bytes and backing file are committed before this
                // point. Clear volatile RAM, then use Renode's ordinary machine
                // reset request so CPUs, timers, NVMC, UART and other volatile
                // peripherals reset from the normal vector. faultFired is not
                // reset, making the configured cut one-shot.
                faultFired = true;
                powerLossCount++;
                var faultRecord = string.Format(CultureInfo.InvariantCulture,
                    "fault=power-loss after_op={0}", operation);
                traceWriter.WriteLine(faultRecord);
                this.Log(LogLevel.Warning, faultRecord);

                // Preserve evidence at the instant of the boundary. Recovery
                // firmware may legitimately touch the same page after reset,
                // so the final flash image alone cannot prove what had already
                // committed when power was lost.
                var evidenceDirectory = Path.GetDirectoryName(tracePath);
                var evidencePath = Path.Combine(evidenceDirectory, "fault-operation.txt");
                var snapshotPath = Path.Combine(evidenceDirectory,
                    "fault-committed-flash.bin");
                backingStream.Flush();
                File.WriteAllText(evidencePath, string.Format(
                    CultureInfo.InvariantCulture,
                    "operation={0}{5}type={1}{5}address=0x{2:X8}{5}length={3}{5}" +
                    "before_sha256={4}{5}after_sha256={6}{5}",
                    operation, type, address, length, Sha256(before),
                    Environment.NewLine, Sha256(after)));
                File.Copy(backingPath, snapshotPath, true);
                ram.ClearForPowerLoss();
                machine.RequestReset();
            }
        }

        private static string Sha256(byte[] bytes)
        {
            using(var sha256 = SHA256.Create())
            {
                return BitConverter.ToString(sha256.ComputeHash(bytes))
                    .Replace("-", string.Empty).ToLowerInvariant();
            }
        }

        private bool writeEnabled;
        private bool eraseEnabled;
        private bool traceEnabled;
        private bool faultFired;
        private long operation;
        private long powerLossCount;
        private long faultAfterOperation;
        private string backingPath;
        private string tracePath;
        private FileStream backingStream;
        private StreamWriter traceWriter;

        private readonly IMachine machine;
        private readonly PowerLossRam ram;
        private readonly int pageSize;
        private readonly object sync = new object();

        private const byte ErasedValue = 0xFF;
    }
}

namespace Antmicro.Renode.Peripherals.MTD
{
    // Only the NVMC surface used by upstream nRF52840 nrfx_nvmc is modeled.
    // READY/READYNEXT are synchronous because a completed operation is the
    // explicit fault boundary in this spike.
    public sealed class FaultInjectingNRF52840NVMC : IDoubleWordPeripheral, IKnownSize
    {
        public FaultInjectingNRF52840NVMC(
            Antmicro.Renode.Peripherals.Memory.FaultInjectingFlash flash)
        {
            this.flash = flash;
            Reset();
        }

        public uint ReadDoubleWord(long offset)
        {
            switch((Registers)offset)
            {
                case Registers.Ready:
                case Registers.ReadyNext:
                    return 1;
                case Registers.Config:
                    return mode;
                case Registers.InstructionCacheConfiguration:
                    return instructionCacheConfiguration;
                case Registers.InstructionCacheHit:
                case Registers.InstructionCacheMiss:
                    return 0;
                default:
                    return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch((Registers)offset)
            {
                case Registers.Config:
                    mode = value & 0x3;
                    flash.SetWriteEnabled(mode == WriteMode);
                    flash.SetEraseEnabled(mode == EraseMode);
                    break;
                case Registers.ErasePage:
                    flash.ErasePage(value);
                    break;
                case Registers.EraseAll:
                    if(value == 1)
                    {
                        flash.EraseAll();
                    }
                    break;
                case Registers.EraseUicr:
                    // The OTA spike does not map UICR as writable flash. Keep
                    // the command observable but do not modify code flash.
                    break;
                case Registers.InstructionCacheConfiguration:
                    instructionCacheConfiguration = value;
                    break;
            }
        }

        public void Reset()
        {
            mode = ReadOnlyMode;
            instructionCacheConfiguration = 0;
            flash.SetWriteEnabled(false);
            flash.SetEraseEnabled(false);
        }

        public long Size => 0x1000;

        private uint mode;
        private uint instructionCacheConfiguration;

        private readonly Antmicro.Renode.Peripherals.Memory.FaultInjectingFlash flash;

        private const uint ReadOnlyMode = 0;
        private const uint WriteMode = 1;
        private const uint EraseMode = 2;

        private enum Registers : long
        {
            Ready = 0x400,
            ReadyNext = 0x408,
            Config = 0x504,
            ErasePage = 0x508,
            EraseAll = 0x50C,
            EraseUicr = 0x514,
            InstructionCacheConfiguration = 0x540,
            InstructionCacheHit = 0x548,
            InstructionCacheMiss = 0x54C,
        }
    }
}
