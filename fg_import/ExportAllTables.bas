Attribute VB_Name = "ExportAllTables"
Option Compare Database
Option Explicit

' BFL Smart Factory - export EVERY table of this Access file to CSV (UTF-8, dates as yyyy-mm-dd hh:nn:ss)
' Output folder: <folder of this .accdb>\FG_CSV_Export\  -> zip it and upload in FG Stock > Import Access
Public Sub ExportAllTablesToCSV()
    Dim db As DAO.Database, td As DAO.TableDef, rs As DAO.Recordset
    Dim folder As String, n As Long, i As Integer, ln As String, st As Object
    folder = CurrentProject.Path & "\FG_CSV_Export\"
    If Dir(folder, vbDirectory) = "" Then MkDir folder
    Set db = CurrentDb
    For Each td In db.TableDefs
        If Left$(td.Name, 4) <> "MSys" And Left$(td.Name, 1) <> "~" And (td.Attributes And dbSystemObject) = 0 Then
            Set st = CreateObject("ADODB.Stream")
            st.Type = 2: st.Charset = "utf-8": st.Open
            Set rs = db.OpenRecordset("SELECT * FROM [" & td.Name & "]", dbOpenSnapshot)
            ln = ""
            For i = 0 To rs.Fields.Count - 1
                If i > 0 Then ln = ln & ","
                ln = ln & Q(rs.Fields(i).Name)
            Next
            st.WriteText ln & vbCrLf
            Do While Not rs.EOF
                ln = ""
                For i = 0 To rs.Fields.Count - 1
                    If i > 0 Then ln = ln & ","
                    ln = ln & Q(V2S(rs.Fields(i).Value))
                Next
                st.WriteText ln & vbCrLf
                rs.MoveNext
            Loop
            rs.Close
            st.SaveToFile folder & td.Name & ".csv", 2
            st.Close
            n = n + 1
            DoEvents
        End If
    Next
    MsgBox n & " tables exported to " & folder, vbInformation, "Export done"
End Sub

Private Function Q(ByVal s As String) As String
    Q = """" & Replace(s, """", """""") & """"
End Function

Private Function V2S(v As Variant) As String
    If IsNull(v) Or IsEmpty(v) Then
        V2S = ""
    ElseIf IsArray(v) Then
        V2S = ""                          ' pictures / attachments are not exported
    ElseIf VarType(v) = vbDate Then
        V2S = Year(v) & "-" & Right$("0" & Month(v), 2) & "-" & Right$("0" & Day(v), 2) & " " & _
              Right$("0" & Hour(v), 2) & ":" & Right$("0" & Minute(v), 2) & ":" & Right$("0" & Second(v), 2)
    Else
        V2S = CStr(v)
    End If
End Function
