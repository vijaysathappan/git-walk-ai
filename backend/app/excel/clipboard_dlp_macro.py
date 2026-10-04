"""VBA source for the local clipboard-DLP module embedded into every
workbook Git Walk issues locally (upload-and-provision, work-on-workbook,
and the local-sanitizer's re-injection cycle -- see vba_writer.embed_vba_project
and its call sites). Deters copying workbook data into external applications
while leaving copy/paste within the workbook itself unaffected.

v1 scope: Auto_Open/Auto_Close only (fires on file open/close). The full
Workbook_Activate/Deactivate/BeforeClose event set would require a VBA
"document module" (ThisWorkbook) -- a binary structure this project's
MS-CFB writer has never implemented or validated in real Excel, unlike the
plain procedural module used here. A deliberate, named gap for a fast-follow
once that support exists and has been manually verified.

Known, permanent limitations (not hidden, set as expectation): this is a
deterrent, not a hard security boundary. It blocks the standard Ctrl+C/X/V
and right-click paths only -- it does not stop the Ribbon's Copy/Paste
buttons, screen-capture/OCR, or a user who declines "Enable Macros" at open.
"""

MODULE_NAME = "mod_VirtualClipboard"

MODULE_SOURCE = r"""Option Explicit

' ==============================================================================
' MODULE: mod_VirtualClipboard
' PURPOSE: Private In-Memory Virtual Clipboard System to Block External Pasting
' ==============================================================================

#If VBA7 Then
    Private Declare PtrSafe Function OpenClipboard Lib "user32" (ByVal hwnd As LongPtr) As Long
    Private Declare PtrSafe Function EmptyClipboard Lib "user32" () As Long
    Private Declare PtrSafe Function CloseClipboard Lib "user32" () As Long
    Private Declare PtrSafe Function GlobalAlloc Lib "kernel32" (ByVal uFlags As Long, ByVal dwBytes As LongPtr) As LongPtr
    Private Declare PtrSafe Function GlobalLock Lib "kernel32" (ByVal hMem As LongPtr) As LongPtr
    Private Declare PtrSafe Function GlobalUnlock Lib "kernel32" (ByVal hMem As LongPtr) As Long
    Private Declare PtrSafe Function lstrcpy Lib "kernel32" Alias "lstrcpyA" (ByVal lpString1 As Any, ByVal lpString2 As String) As LongPtr
    Private Declare PtrSafe Function SetClipboardData Lib "user32" (ByVal uFormat As Long, ByVal hMem As LongPtr) As LongPtr
#Else
    Private Declare Function OpenClipboard Lib "user32" (ByVal hwnd As Long) As Long
    Private Declare Function EmptyClipboard Lib "user32" () As Long
    Private Declare Function CloseClipboard Lib "user32" () As Long
    Private Declare Function GlobalAlloc Lib "kernel32" (ByVal uFlags As Long, ByVal dwBytes As Long) As Long
    Private Declare Function GlobalLock Lib "kernel32" (ByVal hMem As Long) As Long
    Private Declare Function GlobalUnlock Lib "kernel32" (ByVal hMem As Long) As Long
    Private Declare Function lstrcpy Lib "kernel32" Alias "lstrcpyA" (ByVal lpString1 As Any, ByVal lpString2 As String) As Long
    Private Declare Function SetClipboardData Lib "user32" (ByVal uFormat As Long, ByVal hMem As Long) As Long
#End If

Private Const GMEM_MOVEABLE As Long = &H2
Private Const CF_TEXT As Long = 1
Private Const SANITIZED_WARNING As String = "[Git Walk: external pasting of workbook data is disabled]"

Private Type VirtualClipboard
    Values As Variant
    Formulas As Variant
    NumberFormats As Variant
    RowCount As Long
    ColCount As Long
    SourceSheet As Worksheet
    SourceAddress As String
    IsCutOperation As Boolean
    HasData As Boolean
End Type

Private m_ClipStore As VirtualClipboard

' ==============================================================================
' HOOK MANAGEMENT (ENABLE / DISABLE)
' ==============================================================================

Public Sub EnableVirtualClipboard()
    Application.OnKey "^c", "Action_InterceptCopy"
    Application.OnKey "^x", "Action_InterceptCut"
    Application.OnKey "^v", "Action_InterceptPaste"
    Application.OnKey "+{INSERT}", "Action_InterceptPaste"
    Application.OnKey "^{INSERT}", "Action_InterceptCopy"

    ToggleContextMenuItems False
End Sub

Public Sub DisableVirtualClipboard()
    Application.OnKey "^c"
    Application.OnKey "^x"
    Application.OnKey "^v"
    Application.OnKey "+{INSERT}"
    Application.OnKey "^{INSERT}"

    ToggleContextMenuItems True
    FlushInternalBuffer
End Sub

Private Sub ToggleContextMenuItems(ByVal EnableState As Boolean)
    Dim barNames As Variant
    Dim i As Long
    Dim ctrl As CommandBarControl

    barNames = Array("Cell", "Row", "Column")

    On Error Resume Next
    For i = LBound(barNames) To UBound(barNames)
        For Each ctrl In Application.CommandBars(barNames(i)).Controls
            Select Case ctrl.ID
                Case 19, 21, 22, 755 ' Copy, Cut, Paste, Paste Special
                    ctrl.Enabled = EnableState
            End Select
        Next ctrl
    Next i
    On Error GoTo 0
End Sub

' ==============================================================================
' CORE OPERATIONS: COPY, CUT, PASTE
' ==============================================================================

Public Sub Action_InterceptCopy()
    CaptureRangeToMemory False
End Sub

Public Sub Action_InterceptCut()
    CaptureRangeToMemory True
End Sub

Private Sub CaptureRangeToMemory(ByVal IsCut As Boolean)
    If TypeName(Selection) <> "Range" Then Exit Sub

    Dim src As Range
    Set src = Selection

    With m_ClipStore
        .RowCount = src.Rows.Count
        .ColCount = src.Columns.Count
        .SourceAddress = src.Address
        Set .SourceSheet = src.Worksheet
        .IsCutOperation = IsCut

        If .RowCount = 1 And .ColCount = 1 Then
            ReDim .Values(1 To 1, 1 To 1)
            ReDim .Formulas(1 To 1, 1 To 1)
            ReDim .NumberFormats(1 To 1, 1 To 1)
            .Values(1, 1) = src.Value2
            .Formulas(1, 1) = src.FormulaR1C1
            .NumberFormats(1, 1) = src.NumberFormat
        Else
            .Values = src.Value2
            .Formulas = src.FormulaR1C1
            .NumberFormats = src.NumberFormat
        End If

        .HasData = True
    End With

    SanitizeOSClipboard SANITIZED_WARNING

    If IsCut Then
        Application.StatusBar = "Cut operation captured internally. Ready to paste."
    Else
        Application.StatusBar = "Copy operation captured internally. Ready to paste."
    End If
End Sub

Public Sub Action_InterceptPaste()
    If Not m_ClipStore.HasData Then
        Exit Sub
    End If

    If TypeName(Selection) <> "Range" Then Exit Sub

    Dim destTopLeft As Range
    Dim targetRange As Range
    Set destTopLeft = Selection.Cells(1, 1)

    On Error GoTo PasteErrorHandler
    Application.ScreenUpdating = False
    Application.EnableEvents = False

    Set targetRange = destTopLeft.Resize(m_ClipStore.RowCount, m_ClipStore.ColCount)

    targetRange.NumberFormat = m_ClipStore.NumberFormats
    targetRange.FormulaR1C1 = m_ClipStore.Formulas

    If m_ClipStore.IsCutOperation Then
        If Not (m_ClipStore.SourceSheet Is Nothing) Then
            m_ClipStore.SourceSheet.Range(m_ClipStore.SourceAddress).Clear
        End If
        FlushInternalBuffer
        Application.StatusBar = False
    End If

    Application.ScreenUpdating = True
    Application.EnableEvents = True
    Exit Sub

PasteErrorHandler:
    Application.ScreenUpdating = True
    Application.EnableEvents = True
    MsgBox "Failed to complete internal paste: " & Err.Description, vbCritical, "Paste Error"
End Sub

Private Sub FlushInternalBuffer()
    With m_ClipStore
        .Values = Empty
        .Formulas = Empty
        .NumberFormats = Empty
        .RowCount = 0
        .ColCount = 0
        Set .SourceSheet = Nothing
        .SourceAddress = vbNullString
        .IsCutOperation = False
        .HasData = False
    End With
End Sub

' ==============================================================================
' OS CLIPBOARD SANITIZER (WIN32 API)
' ==============================================================================

Private Sub SanitizeOSClipboard(ByVal warningPayload As String)
    #If VBA7 Then
        Dim hGlobal As LongPtr
        Dim pGlobal As LongPtr
    #Else
        Dim hGlobal As Long
        Dim pGlobal As Long
    #End If

    If OpenClipboard(0&) <> 0 Then
        EmptyClipboard

        hGlobal = GlobalAlloc(GMEM_MOVEABLE, Len(warningPayload) + 1)
        If hGlobal <> 0 Then
            pGlobal = GlobalLock(hGlobal)
            If pGlobal <> 0 Then
                lstrcpy pGlobal, warningPayload
                GlobalUnlock hGlobal
                SetClipboardData CF_TEXT, hGlobal
            End If
        End If
        CloseClipboard
    End If
End Sub

' ==============================================================================
' LIFECYCLE (v1: Auto_Open/Auto_Close only -- see module docstring)
' ==============================================================================

Public Sub Auto_Open()
    Call EnableVirtualClipboard
End Sub

Public Sub Auto_Close()
    Call DisableVirtualClipboard
End Sub
"""
