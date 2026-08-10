// SPDX-License-Identifier: MIT
//
// Opt-in volatile RAM backed by Renode's native mapped-memory implementation.
// The reference proof continues to select PowerLossRam in platform.repl.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;

using Antmicro.Renode.Core;
using Antmicro.Renode.Peripherals.Bus;

using Endianess = ELFSharp.ELF.Endianess;

namespace Antmicro.Renode.Peripherals.Memory
{
    public sealed class NativePowerLossRam : IMemory, IMapped,
        IEndiannessAware, IPowerLossRam, IDisposable
    {
        public NativePowerLossRam(IMachine machine, ulong size)
        {
            if(size == 0 || size > long.MaxValue)
            {
                throw new ArgumentOutOfRangeException(nameof(size));
            }
            memory = new MappedMemory(machine, checked((long)size));
        }

        public byte ReadByte(long offset) => memory.ReadByte(offset);
        public ushort ReadWord(long offset) => memory.ReadWord(offset);
        public uint ReadDoubleWord(long offset) => memory.ReadDoubleWord(offset);
        public ulong ReadQuadWord(long offset) => memory.ReadQuadWord(offset);

        public void WriteByte(long offset, byte value) =>
            memory.WriteByte(offset, value);

        public void WriteWord(long offset, ushort value) =>
            memory.WriteWord(offset, value);

        public void WriteDoubleWord(long offset, uint value) =>
            memory.WriteDoubleWord(offset, value);

        public void WriteQuadWord(long offset, ulong value) =>
            memory.WriteQuadWord(offset, value);

        public byte[] ReadBytes(long offset, int count,
            IPeripheral context = null) => memory.ReadBytes(offset, count, context);

        public void WriteBytes(long offset, byte[] bytes, int startingIndex,
            int count, IPeripheral context = null) =>
            memory.WriteBytes(offset, bytes, startingIndex, count, context);

        public void ClearForPowerLoss()
        {
            memory.ZeroAll();
            powerLossClearCount++;
        }

        public void ZeroAll()
        {
            memory.ZeroAll();
        }

        public void FillDeterministically(int seed)
        {
            var random = new Random(seed);
            var bytes = new byte[checked((int)Size)];
            random.NextBytes(bytes);
            memory.WriteBytes(0, bytes);
        }

        public void LoadRam(string path)
        {
            var bytes = File.ReadAllBytes(path);
            if(bytes.LongLength != Size)
            {
                throw new ArgumentException(string.Format(
                    CultureInfo.InvariantCulture,
                    "RAM image '{0}' has {1} bytes, expected {2}",
                    path, bytes.LongLength, Size));
            }
            memory.WriteBytes(0, bytes);
        }

        public void SaveRam(string path)
        {
            File.WriteAllBytes(path, memory.ReadBytes(0, checked((int)Size)));
        }

        public string Sha256()
        {
            using(var sha256 = SHA256.Create())
            {
                return BitConverter.ToString(sha256.ComputeHash(
                    memory.ReadBytes(0, checked((int)Size))))
                    .Replace("-", string.Empty).ToLowerInvariant();
            }
        }

        public void Reset()
        {
            // An ordinary machine reset retains RAM. Simulated power loss is
            // the only path that explicitly calls ClearForPowerLoss().
        }

        public void Dispose()
        {
            memory.Dispose();
        }

        public long Size => memory.Size;
        public long PowerLossClearCount => powerLossClearCount;
        public IEnumerable<IMappedSegment> MappedSegments =>
            memory.MappedSegments;
        public Endianess Endianness => memory.Endianness;

        private long powerLossClearCount;
        private readonly MappedMemory memory;
    }
}
