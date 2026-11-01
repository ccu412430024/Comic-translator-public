// GUI subsystem prevents a console window. Only framed JSON goes to stdout.
using System;
using System.Diagnostics;
using System.IO;

internal static class ComicReaderHost {
    const int Limit = 8192;
    static byte[] ReadExact(Stream input, int size) {
        byte[] data = new byte[size];
        int offset = 0;
        while (offset < size) {
            int count = input.Read(data, offset, size - offset);
            if (count == 0) throw new IOException();
            offset += count;
        }
        return data;
    }
    static void Reply(Stream output, string text) {
        byte[] data = System.Text.Encoding.UTF8.GetBytes(text);
        output.Write(BitConverter.GetBytes(data.Length), 0, 4);
        output.Write(data, 0, data.Length);
        output.Flush();
    }
    [STAThread]
    static void Main(string[] args) {
        using (Stream output = Console.OpenStandardOutput()) {
            try {
                if (args.Length < 1 || !System.Text.RegularExpressions.Regex.IsMatch(args[0], @"\Achrome-extension://[a-p]{32}/\z")) {
                    Reply(output, "{\"ok\":false,\"reason\":\"origin_denied\"}"); return;
                }
                Stream input = Console.OpenStandardInput();
                byte[] header = ReadExact(input, 4);
                int length = BitConverter.ToInt32(header, 0);
                if (length <= 0 || length > Limit) throw new IOException();
                byte[] body = ReadExact(input, length);
                string root = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", ".."));
                var info = new ProcessStartInfo(Path.Combine(root, ".venv", "Scripts", "python.exe"));
                info.Arguments = "-u \"" + Path.Combine(root, "scripts", "native_reader_host.py") + "\" \"" + args[0] + "\"";
                info.WorkingDirectory = root;
                info.UseShellExecute = false;
                info.CreateNoWindow = true;
                info.RedirectStandardInput = true;
                info.RedirectStandardOutput = true;
                using (var process = Process.Start(info)) {
                    process.StandardInput.BaseStream.Write(header, 0, 4);
                    process.StandardInput.BaseStream.Write(body, 0, body.Length);
                    process.StandardInput.BaseStream.Flush();
                    process.StandardInput.Close();
                    byte[] replyHeader = ReadExact(process.StandardOutput.BaseStream, 4);
                    int replySize = BitConverter.ToInt32(replyHeader, 0);
                    if (replySize <= 0 || replySize > Limit) throw new IOException();
                    byte[] reply = ReadExact(process.StandardOutput.BaseStream, replySize);
                    output.Write(replyHeader, 0, 4);output.Write(reply, 0, reply.Length);output.Flush();
                    process.WaitForExit();
                }
            } catch {
                Reply(output, "{\"ok\":false,\"reason\":\"host_failed\"}");
            }
        }
    }
}
