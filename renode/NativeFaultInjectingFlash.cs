// SPDX-License-Identifier: MIT
//
// Experimental fault-injecting flash backed by Renode's native mapped memory.
// The production proof continues to use FaultInjectingFlash until differential
// equivalence has been established.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;

using Antmicro.Migrant;
using Antmicro.Migrant.Hooks;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.CPU;

using Endianess = ELFSharp.ELF.Endianess;

namespace Antmicro.Renode.Peripherals.Memory
{
    public sealed class NativeFaultInjectingFlash : IMemory, IMapped,
        IEndiannessAware, IDisposable
    {
        public NativeFaultInjectingFlash(IMachine machine, PowerLossRam ram,
            ulong size, int pageSize)
            : this(machine, (IPowerLossRam)ram, size, pageSize)
        {
        }

        public NativeFaultInjectingFlash(IMachine machine,
            NativePowerLossRam ram, ulong size, int pageSize)
            : this(machine, (IPowerLossRam)ram, size, pageSize)
        {
        }

        private NativeFaultInjectingFlash(IMachine machine, IPowerLossRam ram,
            ulong size, int pageSize)
        {
            if(pageSize <= 0 || size % (ulong)pageSize != 0)
            {
                throw new ArgumentException(
                    "Flash size must be a multiple of pageSize");
            }
            if(size > long.MaxValue)
            {
                throw new ArgumentOutOfRangeException(nameof(size));
            }

            this.machine = machine;
            this.ram = ram;
            this.pageSize = pageSize;
            memory = new MappedMemory(machine, checked((long)size));
        }

        public byte ReadByte(long offset)
        {
            dataReadCount++;
            return memory.ReadByte(offset);
        }

        public ushort ReadWord(long offset)
        {
            dataReadCount++;
            return memory.ReadWord(offset);
        }

        public uint ReadDoubleWord(long offset)
        {
            dataReadCount++;
            return memory.ReadDoubleWord(offset);
        }

        public ulong ReadQuadWord(long offset)
        {
            dataReadCount++;
            return memory.ReadQuadWord(offset);
        }

        public void WriteByte(long offset, byte value)
        {
            Program(offset, new[] { value });
        }

        public void WriteWord(long offset, ushort value)
        {
            Program(offset, BitConverter.GetBytes(value));
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            Program(offset, BitConverter.GetBytes(value));
        }

        public void WriteQuadWord(long offset, ulong value)
        {
            Program(offset, BitConverter.GetBytes(value));
        }

        public byte[] ReadBytes(long offset, int count, IPeripheral context = null)
        {
            return memory.ReadBytes(offset, count, context);
        }

        public void WriteBytes(long offset, byte[] bytes, int startingIndex,
            int count, IPeripheral context = null)
        {
            var requested = new byte[count];
            Array.Copy(bytes, startingIndex, requested, 0, count);
            Program(offset, requested);
        }

        public void SetWriteEnabled(bool enabled)
        {
            writeEnabled = enabled;
            if(enabled && !writeInterceptionInstalled)
            {
                RefreshDataAccessMapping();
            }
        }

        public void SetEraseEnabled(bool enabled)
        {
            eraseEnabled = enabled;
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
                this.Log(LogLevel.Error,
                    "Ignoring invalid page erase at 0x{0:X8}", address);
                return;
            }

            lock(sync)
            {
                var captureEvidence = NextOperationTriggersFault();
                var before = captureEvidence
                    ? memory.ReadBytes(address, pageSize) : null;
                memory.SetRange(address, pageSize, ErasedValue);
                var after = memory.ReadBytes(address, pageSize);
                Persist(address, after, 0, pageSize);
                CompletedOperation("erase", address, pageSize, before,
                    captureEvidence ? after : null);
            }
        }

        public void EraseAll()
        {
            if(!eraseEnabled)
            {
                this.Log(LogLevel.Warning,
                    "Ignoring mass erase: NVMC is not in erase mode");
                return;
            }

            for(var address = 0L; address < Size; address += pageSize)
            {
                ErasePage(address);
            }
        }

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
                backingStream?.Dispose();
                memory.WriteBytes(0, bytes);
                backingPath = Path.GetFullPath(path);
                backingStream = new FileStream(backingPath, FileMode.Open,
                    FileAccess.Write, FileShare.Read, 1);
                hostResourcesRequireRebind = false;
                RefreshDataAccessMapping();
            }
        }

        public void SaveFlash(string path)
        {
            lock(sync)
            {
                File.WriteAllBytes(path, memory.ReadBytes(0, checked((int)Size)));
            }
        }

        public void BeginTraceFromEnvironment(string path)
        {
            var value = Environment.GetEnvironmentVariable("FAULT_AFTER_OPERATION");
            long faultAfter = 0;
            if(!string.IsNullOrWhiteSpace(value)
                && !long.TryParse(value, NumberStyles.None,
                    CultureInfo.InvariantCulture, out faultAfter))
            {
                throw new ArgumentException(
                    "FAULT_AFTER_OPERATION must be an unsigned decimal operation number");
            }
            value = Environment.GetEnvironmentVariable("CHECKPOINT_AFTER_OPERATION");
            long checkpointAfter = 0;
            if(!string.IsNullOrWhiteSpace(value)
                && !long.TryParse(value, NumberStyles.None,
                    CultureInfo.InvariantCulture, out checkpointAfter))
            {
                throw new ArgumentException(
                    "CHECKPOINT_AFTER_OPERATION must be an unsigned decimal operation number");
            }
            BeginTrace(path, faultAfter);
            checkpointAfterOperation = checkpointAfter;
        }

        public void BeginTrace(string path, long faultAfter)
        {
            if(faultAfter < 0)
            {
                throw new ArgumentOutOfRangeException(nameof(faultAfter));
            }

            lock(sync)
            {
                traceWriter?.Dispose();
                tracePath = Path.GetFullPath(path);
                CreateParentDirectory(tracePath);
                traceWriter = new StreamWriter(new FileStream(tracePath,
                    FileMode.Create, FileAccess.Write, FileShare.Read));
                traceWriter.AutoFlush = true;
                operation = 0;
                powerLossCount = 0;
                faultAfterOperation = faultAfter;
                faultFired = false;
                checkpointAfterOperation = 0;
                traceEnabled = true;
            }
        }

        public void EndTrace()
        {
            lock(sync)
            {
                traceEnabled = false;
                traceWriter?.Dispose();
                traceWriter = null;
            }
        }

        public void CloseHostResources()
        {
            lock(sync)
            {
                traceWriter?.Dispose();
                traceWriter = null;
                backingStream?.Dispose();
                backingStream = null;
                hostResourcesRequireRebind = true;
            }
        }

        public void RebindHostResources(string backing, string trace,
            long faultAfter, long expectedOperation)
        {
            RebindHostResourcesExperimental(backing, trace, faultAfter,
                expectedOperation, false, false);
        }

        public void RebindHostResourcesExperimental(string backing, string trace,
            long faultAfter, long expectedOperation, bool bufferTrace,
            bool bufferBacking)
        {
            lock(sync)
            {
                if(operation != expectedOperation)
                {
                    throw new InvalidOperationException(string.Format(
                        CultureInfo.InvariantCulture,
                        "checkpoint operation is {0}, expected {1}",
                        operation, expectedOperation));
                }
                if(faultAfter <= operation || faultFired || powerLossCount != 0)
                {
                    throw new InvalidOperationException(
                        "checkpoint is not a clean pre-fault lineage state");
                }

                traceWriter?.Dispose();
                backingStream?.Dispose();
                backingPath = Path.GetFullPath(backing);
                tracePath = Path.GetFullPath(trace);
                CreateParentDirectory(backingPath);
                CreateParentDirectory(tracePath);
                using(var stream = new FileStream(backingPath, FileMode.CreateNew,
                    FileAccess.Write, FileShare.Read))
                {
                    var bytes = memory.ReadBytes(0, checked((int)Size));
                    stream.Write(bytes, 0, bytes.Length);
                }
                backingStream = new FileStream(backingPath, FileMode.Open,
                    FileAccess.Write, FileShare.Read, bufferBacking ? 4096 : 1);
                traceWriter = new StreamWriter(new FileStream(tracePath,
                    FileMode.CreateNew, FileAccess.Write, FileShare.Read));
                traceWriter.AutoFlush = !bufferTrace;
                faultAfterOperation = faultAfter;
                checkpointAfterOperation = 0;
                traceEnabled = true;
                hostResourcesRequireRebind = false;
                RefreshDataAccessMapping();
            }
        }

        public void Reset()
        {
            // Internal flash and fault lineage survive a machine reset. NVMC
            // resets its access mode separately. The experimental tlib keeps
            // its write-only page classification across CPU reset.
        }

        public void Dispose()
        {
            CloseHostResources();
            memory.Dispose();
        }

        public long OperationCount => operation;
        public long PowerLossCount => powerLossCount;
        public long FaultAfterOperation => faultAfterOperation;
        public bool FaultFired => faultFired;
        public long NativeDataReadCount => dataReadCount;
        public long Size => memory.Size;
        public IEnumerable<IMappedSegment> MappedSegments => memory.MappedSegments;
        public Endianess Endianness => memory.Endianness;

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
                this.Log(LogLevel.Error,
                    "Ignoring invalid program at 0x{0:X8}", offset);
                return;
            }

            lock(sync)
            {
                var captureEvidence = NextOperationTriggersFault();
                var current = memory.ReadBytes(offset, requested.Length);
                var before = captureEvidence ? (byte[])current.Clone() : null;
                for(var i = 0; i < requested.Length; ++i)
                {
                    current[i] &= requested[i];
                }
                memory.WriteBytes(offset, current);
                Persist(offset, current, 0, current.Length);
                CompletedOperation("program", offset, requested.Length, before,
                    captureEvidence ? current : null);
            }
        }

        private void RefreshDataAccessMapping()
        {
            if(writeInterceptionInstalled)
            {
                return;
            }
            foreach(var cpu in machine.SystemBus.GetCPUs()
                .OfType<ICPUWithMappedMemory>())
            {
                var cpuType = cpu.GetType();
                var set = cpuType.GetMethod("SetMappedMemoryWritesViaIo",
                    BindingFlags.Instance | BindingFlags.Public);
                if(set == null)
                {
                    throw new InvalidOperationException(
                        "experimental Renode build lacks ROMD mapped-memory interception");
                }
                set.Invoke(cpu, new object[] { 0UL, (ulong)Size });
            }
            writeInterceptionInstalled = true;
        }

        private bool NextOperationTriggersFault()
        {
            return traceEnabled && !faultFired
                && faultAfterOperation == operation + 1;
        }

        private void Persist(long offset, byte[] source, int sourceOffset,
            int count)
        {
            if(hostResourcesRequireRebind || backingStream == null)
            {
                throw new InvalidOperationException(
                    "LoadFlash must establish a nonvolatile backing file before guest execution");
            }

            backingStream.Position = offset;
            backingStream.Write(source, sourceOffset, count);
        }

        private void CompletedOperation(string type, long address, int length,
            byte[] before, byte[] after)
        {
            if(!traceEnabled)
            {
                return;
            }

            operation++;
            traceWriter.WriteLine(string.Format(CultureInfo.InvariantCulture,
                "op={0} type={1} address=0x{2:X8} length={3}",
                operation, type, address, length));

            if(checkpointAfterOperation == operation)
            {
                traceWriter.Flush();
                checkpointAfterOperation = 0;
                machine.PauseAndRequestEmulationPause(precise: true);
            }

            if(faultAfterOperation == operation && !faultFired)
            {
                if(before == null || after == null)
                {
                    throw new InvalidOperationException(
                        "selected fault operation lacks before/after evidence");
                }
                faultFired = true;
                powerLossCount++;
                var faultRecord = string.Format(CultureInfo.InvariantCulture,
                    "fault=power-loss after_op={0}", operation);
                traceWriter.WriteLine(faultRecord);
                traceWriter.Flush();
                this.Log(LogLevel.Warning, faultRecord);

                var evidenceDirectory = Path.GetDirectoryName(tracePath);
                var evidencePath = Path.Combine(evidenceDirectory,
                    "fault-operation.txt");
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

        [PostDeserialization]
        private void RequireHostResourceRebind()
        {
            backingStream = null;
            traceWriter = null;
            hostResourcesRequireRebind = true;
            // Native CPU mappings are reconstructed after snapshot restore;
            // branch rebind reinstalls the ROMD descriptor before execution.
            writeInterceptionInstalled = false;
        }

        private static void CreateParentDirectory(string path)
        {
            var directory = Path.GetDirectoryName(path);
            if(!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }
        }

        private bool writeEnabled;
        private bool eraseEnabled;
        private bool traceEnabled;
        private bool faultFired;
        private bool hostResourcesRequireRebind;
        private bool writeInterceptionInstalled;
        private long operation;
        private long powerLossCount;
        private long faultAfterOperation;
        private long checkpointAfterOperation;
        private long dataReadCount;
        private string backingPath;
        private string tracePath;
        [Transient]
        private FileStream backingStream;
        [Transient]
        private StreamWriter traceWriter;

        private readonly IMachine machine;
        private readonly IPowerLossRam ram;
        private readonly int pageSize;
        private readonly MappedMemory memory;
        private readonly object sync = new object();

        private const byte ErasedValue = 0xFF;
    }
}

namespace Antmicro.Renode.Peripherals.MTD
{
    public sealed class NativeFaultInjectingNRF52840NVMC : IDoubleWordPeripheral,
        IKnownSize
    {
        public NativeFaultInjectingNRF52840NVMC(
            Antmicro.Renode.Peripherals.Memory.NativeFaultInjectingFlash flash)
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
        private readonly Antmicro.Renode.Peripherals.Memory.NativeFaultInjectingFlash flash;

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
            InstructionCacheConfiguration = 0x540,
        }
    }
}
